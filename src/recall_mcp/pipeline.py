"""
Memory Pipeline — Orchestrates Gate → Extract → Store

PATTERN: Pipeline Pattern (Chain of Responsibility)
=====================================================

The Pipeline Pattern connects processing stages in a linear flow:
  Input → Stage 1 → Stage 2 → Stage 3 → Output

Each stage:
  - Receives input from the previous stage
  - Processes it independently
  - Passes output to the next stage

This is identical to:
  - Middleware in Express.js (request → auth → validate → route → response)
  - Image processing in Photoshop (raw → filter → crop → export)
  - ML data pipelines (raw → clean → transform → feature → train)

WHY PIPELINE?
  - Each stage is testable in isolation (unit test the gate without extraction)
  - Each stage is swappable (swap Nemotron for GPT-4o-mini without changing store)
  - Easy to add stages (add "deduplicate" between extract and store)
  - Failure in one stage doesn't crash others (graceful degradation)
"""

from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Optional
from recall_mcp.config import Settings
from recall_mcp.gate import GateDecision
from recall_mcp.registry import build_gate_chain
from recall_mcp.extract import OllamaExtractor, StubExtractor, BaseExtractor
from recall_mcp.store import MemoryStore
from recall_mcp.store.edges import EntityStore
from recall_mcp.store.memory import MemoryStoreV2
from recall_mcp.store.facts import FactStore
from recall_mcp.store.markdown import MarkdownSync
from recall_mcp.graph.entity import EntityDetector
from recall_mcp.graph.edges import GraphWiring
from recall_mcp.graph.traversal import GraphTraversal
from recall_mcp.search.hybrid import HybridSearch
from recall_mcp.embeddings import EmbeddingError, OllamaEmbeddingProvider
from recall_mcp.dream.cycle import DreamCycle
from recall_mcp.gate_backends import GateMetrics

logger = logging.getLogger(__name__)


class MemoryPipeline:
    """
    Orchestrates the full memory pipeline: Gate → Extract → Store.

    The gate uses a fallback chain: DilBERT → OpenAI → Heuristic
    This means it works on EVERY machine, from day one, no config needed.

    Usage:
        pipeline = MemoryPipeline()           # uses default settings
        result = pipeline.capture("text")     # returns dict
        memories = pipeline.search("query")    # returns list of dicts
        pipeline.consolidate()                 # runs decay/promotion
        metrics = pipeline.metrics_summary()   # effectiveness report
    """

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or Settings()

        # ── Layer 1: Gate (pluggable fallback chain) ──────────
        # Build from config or env var RECALL_GATE_BACKENDS
        # Default: dilbert → heuristic (works everywhere)
        # Custom: set env var, e.g. RECALL_GATE_BACKENDS=openai,heuristic
        metrics_db = self.settings.DB_PATH.parent / "metrics.db"
        self.metrics = GateMetrics(db_path=metrics_db)
        self.gate_chain = build_gate_chain(
            settings=self.settings,
            metrics=self.metrics,
        )

        # ── Layer 2: Extract (structured extraction) ───────────
        # Pluggable via extractor backends (same pattern as gate)
        # Currently supports Ollama (local) with stub fallback

        try:
            self.extractor: BaseExtractor = OllamaExtractor(
                model=self.settings.EXTRACT_MODEL,
                base_url=self.settings.OLLAMA_BASE_URL,
            )
        except Exception:
            logger.warning("Ollama extractor unavailable, using stub")
            self.extractor = StubExtractor()
        self.embedding_provider = None
        if self.settings.EMBEDDINGS_ENABLED:
            self.embedding_provider = OllamaEmbeddingProvider(
                base_url=self.settings.OLLAMA_BASE_URL,
                model=self.settings.EMBED_MODEL,
                timeout=self.settings.OLLAMA_TIMEOUT_SECONDS,
                max_concurrency=self.settings.OLLAMA_MAX_CONCURRENCY,
            )
        self._executor = ThreadPoolExecutor(
            max_workers=self.settings.OLLAMA_MAX_CONCURRENCY,
            thread_name_prefix="recall-processing",
        )
        self._processing_slots = threading.BoundedSemaphore(
            self.settings.PROCESSING_QUEUE_LIMIT
        )
        self.nats_sub: object | None = None

        # ── Layer 3: Store (database) ───────────────────────────
        self.store = MemoryStore(
            db_path=self.settings.DB_PATH,
            cold_ttl=self.settings.COLD_TTL,
            active_ttl=self.settings.ACTIVE_TTL,
            persist_ttl=self.settings.PERSIST_TTL,
        )

        # ── V2: Entity Store + Knowledge Graph ─────────────────
        # Unified into the main DB (DB_PATH) so dream-cycle phases can join
        # the entity graph against the `memories` table in a single connection.
        # Previously split into a separate entities.db, which broke
        # entity_sweep/backlink_audit/purge ("no such table: memories").
        self.entity_store = EntityStore(db_path=self.settings.DB_PATH)

        # ── V2: Memory Store V2 Extensions ────────────────────
        self.store_v2 = MemoryStoreV2(v1_store=self.store)

        # ── V2: Fact Store ────────────────────────────────────
        self.fact_store = FactStore(db_path=self.settings.DB_PATH)

        # ── V2: Graph Wiring + Entity Detection ──────────────
        self.graph_wiring = GraphWiring(self.entity_store)
        self.entity_detector = EntityDetector(entity_store=self.entity_store)

        # ── V2: Graph Traversal ────────────────────────────────
        self.graph_traversal = GraphTraversal(self.entity_store)

        # ── V2: Hybrid Search ─────────────────────────────────
        self.hybrid_search = HybridSearch(
            db_path=self.settings.DB_PATH,
            entity_store=self.entity_store,
            embedding_provider=self.embedding_provider,
            fact_store=self.fact_store,
        )

        # ── V2: Dream Cycle ──────────────────────────────────
        self.dream_cycle = DreamCycle(
            entity_store=self.entity_store,
            memory_v2=self.store_v2,
            memory_store=self.store,
            embedding_provider=self.embedding_provider,
            ollama_base_url=self.settings.OLLAMA_BASE_URL,
            fact_store=self.fact_store,
        )

        # ── V2: Markdown Sync ────────────────────────────────
        self.markdown_sync = MarkdownSync(
            self.entity_store, brain_dir=self.settings.BASE_DIR / "brain"
        )

    def capture(
        self,
        text: str,
        source: str = "cli",
        category: Optional[str] = None,
        tier: Optional[str] = None,
        project: Optional[str] = None,
        agent: Optional[str] = None,
    ) -> dict:
        """
        Run the full pipeline on a piece of text.

        PIPELINE FLOW:
          1. Gate classifies (with fallback chain): SKIP → stop, COLD/ACTIVE/PERSIST → continue
          2. Extract summarizes and categorizes
          3. Store persists to SQLite with tier-based TTL
        """
        capture_id = self.store.persist_raw_capture(
            text, source=source, project=project or "", agent=agent or ""
        )
        if not self._processing_slots.acquire(blocking=False):
            self.store.fail_raw_capture(capture_id, "processing capacity exhausted")
            return {
                "id": capture_id,
                "decision": "FAILED",
                "confidence": None,
                "backend": None,
                "fallback_used": False,
                "category": category,
                "tier": tier,
                "summary": text[:200],
                "topics": [],
                "processing_status": "failed",
                "processing_error": "processing capacity exhausted",
            }
        try:
            future = self._executor.submit(
                self._finish_capture,
                capture_id,
                text,
                source,
                category,
                tier,
                project,
                agent,
            )
        except Exception:
            self._processing_slots.release()
            self.store.fail_raw_capture(capture_id, "processing unavailable")
            logger.exception("Unable to schedule capture %s", capture_id)
            return {
                "id": capture_id,
                "decision": "FAILED",
                "processing_status": "failed",
                "processing_error": "processing unavailable",
            }
        future.add_done_callback(lambda _future: self._processing_slots.release())
        try:
            return future.result(timeout=self.settings.CAPTURE_PROCESSING_TIMEOUT)
        except FutureTimeoutError:
            return {
                "id": capture_id,
                "decision": "PENDING",
                "confidence": None,
                "backend": None,
                "fallback_used": False,
                "category": category,
                "tier": tier,
                "summary": text[:200],
                "topics": [],
                "entities": [],
                "new_entities": [],
                "edges_created": 0,
                "processing_status": "pending",
                "embedding_status": "pending"
                if self.embedding_provider
                else "disabled",
                "facts_created": 0,
            }
        except Exception:
            logger.exception("Capture processing failed for %s", capture_id)
            return {
                "id": capture_id,
                "decision": "FAILED",
                "confidence": None,
                "backend": None,
                "fallback_used": False,
                "category": category,
                "tier": tier,
                "summary": text[:200],
                "topics": [],
                "processing_status": "failed",
                "processing_error": "capture processing failed",
            }

    def _finish_capture(
        self,
        capture_id: str,
        text: str,
        source: str,
        category: Optional[str],
        tier: Optional[str],
        project: Optional[str],
        agent: Optional[str],
    ) -> dict:
        """Process a previously persisted raw capture."""
        mem_id: str | None = None
        try:
            gate_result, backend_used, fallback_used = self.gate_chain.classify(text)
            if gate_result.decision == GateDecision.SKIP:
                self.store.complete_raw_capture(capture_id, None, status="skipped")
                return {
                    "id": None,
                    "decision": "SKIP",
                    "confidence": gate_result.confidence,
                    "backend": backend_used,
                    "fallback_used": fallback_used,
                    "category": None,
                    "tier": None,
                    "summary": None,
                    "topics": None,
                    "processing_status": "complete",
                }

            provisional_tier = tier or gate_result.decision.value.lower()
            provisional_category = category or "project"
            mem_id = self.store.store(
                content=text,
                summary=text[:200],
                category=provisional_category,
                tier=provisional_tier,
                key_topics=[],
                source=source,
                project=project or "",
                agent=agent or "",
                processing_status="pending",
                memory_id=capture_id,
            )

            extraction = self.extractor.extract(
                text, source=source, gate_decision=gate_result.decision.value
            )
            final_category = category or extraction.category
            final_tier = tier or extraction.tier
            self.store.update_enrichment(
                mem_id,
                summary=extraction.summary,
                category=final_category,
                tier=final_tier,
                key_topics=extraction.key_topics,
                processing_status="pending" if self.embedding_provider else "complete",
            )

            wiring_result = None
            try:
                wiring_result = self.graph_wiring.wire(
                    text, memory_id=mem_id, source=source
                )
            except Exception as exc:
                logger.warning("Graph wiring failed (non-blocking): %s", exc)

            try:
                facts_created = self._capture_facts(text, source=f"{source}:{mem_id}")
            except Exception as exc:
                logger.warning("Fact extraction failed (non-blocking): %s", exc)
                facts_created = []

            embedding_status = "disabled"
            processing_status = "complete"
            if self.embedding_provider is not None:
                embedded = self._embed_memory(mem_id, text)
                embedding_status = "complete" if embedded else "failed"
                processing_status = "complete" if embedded else "failed"

            self.store.complete_raw_capture(
                capture_id, mem_id, status=processing_status
            )
            return {
                "id": mem_id,
                "decision": gate_result.decision.value,
                "confidence": gate_result.confidence,
                "backend": backend_used,
                "fallback_used": fallback_used,
                "category": final_category,
                "tier": final_tier,
                "summary": extraction.summary,
                "topics": extraction.key_topics,
                "entities": wiring_result.get("entities", []) if wiring_result else [],
                "new_entities": wiring_result.get("new_entities", [])
                if wiring_result
                else [],
                "edges_created": len(wiring_result.get("edges", []))
                if wiring_result
                else 0,
                "processing_status": processing_status,
                "embedding_status": embedding_status,
                "facts_created": len(facts_created),
            }
        except Exception as exc:
            self.store.fail_raw_capture(capture_id, str(exc))
            if mem_id is not None:
                self.store.update_enrichment(
                    mem_id,
                    summary=text[:200],
                    category=category or "project",
                    tier=tier or "active",
                    key_topics=[],
                    processing_status="failed",
                    processing_error=str(exc),
                )
            raise

    def _capture_facts(self, text: str, source: str) -> list[str]:
        """Extract conservative entity facts with source provenance."""
        patterns = (
            (
                "description",
                r"(?P<entity>[A-Z][\w-]+)\s+(?:is|was)\s+(?P<value>[^.!?]{3,160})",
            ),
            ("uses", r"(?P<entity>[A-Z][\w-]+)\s+uses\s+(?P<value>[^.!?]{2,120})"),
            (
                "technology",
                r"(?P<entity>[A-Z][\w-]+)\s+switched\s+to\s+(?P<value>[^.!?]{2,120})",
            ),
            (
                "decision",
                r"(?P<entity>[A-Z][\w-]+)\s+decided\s+(?P<value>[^.!?]{3,160})",
            ),
        )
        fact_ids: list[str] = []
        seen: set[tuple[str, str, str]] = set()
        for claim_key, pattern in patterns:
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                entity = self.entity_store.find_entity(match.group("entity"))
                if not entity:
                    continue
                value = " ".join(match.group("value").split()).strip()
                key = (entity["id"], claim_key, value.lower())
                if not value or key in seen:
                    continue
                seen.add(key)
                fact_ids.append(
                    self.fact_store.assert_fact(
                        entity["id"],
                        claim_key,
                        value,
                        source,
                        confidence=0.8,
                    )
                )
        return fact_ids

    def _embed_memory(self, mem_id: str, text: str) -> bool:
        """Generate and persist an embedding in a background worker."""
        if self.embedding_provider is None:
            return False
        try:
            embedded = self.embedding_provider.embed(text)
            self.store.set_embedding(
                mem_id,
                embedded.to_bytes(),
                model=embedded.model,
                dimensions=embedded.dimensions,
                content_hash=embedded.content_hash,
            )
            return True
        except EmbeddingError as exc:
            logger.warning("Embedding failed for %s: %s", mem_id, exc)
            self.store.mark_embedding_error(mem_id, str(exc))
            return False

    def search(
        self,
        query: str,
        category: Optional[str] = None,
        tier: Optional[str] = None,
        limit: int = 10,
        mode: str = "balanced",
        project: Optional[str] = None,
        agent: Optional[str] = None,
    ) -> list[dict]:
        """Search memories with bounded keyword, vector, balanced, or deep retrieval."""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        if mode not in {"keyword", "vector", "balanced", "deep"}:
            raise ValueError("unsupported search mode")
        bounded_limit = max(1, min(int(limit), self.settings.MAX_RESULTS))
        return self.hybrid_search.search(
            query,
            mode=mode,
            category=category,
            tier=tier,
            limit=bounded_limit,
            project=project,
            agent=agent,
        )

    def get(self, mem_id: str) -> Optional[dict]:
        """Get a specific memory by ID."""
        return self.store.get(mem_id)

    def consolidate(self) -> dict:
        """Run the decay/promotion cycle on stored memories."""
        return self.store.consolidate()

    def delete(self, mem_id: str) -> bool:
        """Delete a specific memory."""
        return self.store.delete(mem_id)

    def metrics_summary(self, hours: int = 24) -> dict:
        """
        Get effectiveness metrics for the gate.

        Use this to answer:
          - Is DilBERT better than heuristics? (compare by_backend)
          - What % of messages get SKIPPED? (skip_rate)
          - Is the fallback working? (fallback_rate)
          - How confident is the gate overall? (avg_confidence)
        """
        return self.metrics.summary(hours=hours)

    # ── V2 Methods ──────────────────────────────────────────────

    def build_context(
        self,
        task: str,
        project: Optional[str] = None,
        agent: Optional[str] = None,
        limit: int = 10,
    ) -> dict:
        """
        Build context for a task using hybrid search + graph traversal.

        This is what agents call before working on a task.
        Returns relevant memories, entities, and open threads.
        """
        return self.hybrid_search.build_context(
            query=task, project=project, agent=agent, limit=limit
        )

    def graph_query(
        self, entity_name: str, depth: int = 1, edge_types: Optional[list[str]] = None
    ) -> dict:
        """
        Traverse the knowledge graph from an entity.
        """
        entity = self.entity_store.find_entity(entity_name)
        if not entity:
            return {"error": f"Entity '{entity_name}' not found"}
        return self.graph_traversal.query(
            entity["id"], depth=depth, edge_types=edge_types
        )

    def entity_get(self, name: str) -> Optional[dict]:
        """Get an entity, including current facts and provenance."""
        entity = self.entity_store.find_entity(name)
        if not entity:
            return None
        result = dict(entity)
        result["facts"] = self.fact_store.get_entity_facts(entity["id"])
        return result

    def dream(self, phases: Optional[list[str]] = None, dry_run: bool = False) -> dict:
        """
        Run the dream cycle.
        """
        return self.dream_cycle.run(phases=phases, dry_run=dry_run)

    def export_brain(self) -> dict:
        """
        Export all entities to the brain markdown repo.
        """
        return self.markdown_sync.export_all()

    def stats(self) -> dict:
        """
        Get comprehensive stats across all V2 subsystems.
        """
        return {
            "memories": self.store.count(),
            "operational": self.store.operational_stats(),
            "entities": self.entity_store.stats(),
            "facts": self.fact_store.stats(),
            "v2": self.store_v2.v2_stats(),
        }
