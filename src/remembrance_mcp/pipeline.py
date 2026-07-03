from __future__ import annotations

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

import logging
from typing import Optional

from remembrance_mcp.chunk.chunk import chunk_text
from remembrance_mcp.config import Settings
from remembrance_mcp.dream.cycle import DreamCycle
from remembrance_mcp.embed.embed import build_embed_chain
from remembrance_mcp.extract import BaseExtractor, OllamaExtractor, StubExtractor
from remembrance_mcp.gate import GateDecision
from remembrance_mcp.gate.backends import GateMetrics
from remembrance_mcp.gate.registry import build_gate_chain
from remembrance_mcp.graph.edges import GraphWiring
from remembrance_mcp.graph.entity import EntityDetector
from remembrance_mcp.graph.traversal import GraphTraversal
from remembrance_mcp.search.hybrid import HybridSearch
from remembrance_mcp.store import MemoryStore
from remembrance_mcp.store.edges import EntityStore
from remembrance_mcp.store.facts import FactStore
from remembrance_mcp.store.markdown import MarkdownSync
from remembrance_mcp.store.memory import MemoryStoreV2

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
        # Build from config or env var REMEMBRANCE_GATE_BACKENDS
        # Default: dilbert → heuristic (works everywhere)
        # Custom: set env var, e.g. REMEMBRANCE_GATE_BACKENDS=openai,heuristic
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

        # ── Embedding (provider-agnostic, non-blocking on write) ──
        # Chain from REMEMBRANCE_EMBED_BACKENDS (default "hash" offline scaffold).
        # Always usable: hash is the ultimate fallback, so capture() can embed
        # even with no Ollama/OpenAI available.
        self.embed_chain = build_embed_chain()

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
            embed_chain=self.embed_chain,  # same model for query as for write
        )

        # ── V2: Dream Cycle ──────────────────────────────────
        self.dream_cycle = DreamCycle(
            entity_store=self.entity_store,
            memory_v2=self.store_v2,
            ollama_base_url=self.settings.OLLAMA_BASE_URL,
            embed_chain=self.embed_chain,  # same model as write/search for backfill
        )

        # ── V2: Markdown Sync ────────────────────────────────
        self.markdown_sync = MarkdownSync(self.entity_store)

    def capture(
        self,
        text: str,
        source: str = "cli",
        category: Optional[str] = None,
        tier: Optional[str] = None,
    ) -> dict:
        """
        Run the full pipeline on a piece of text.

        PIPELINE FLOW:
          1. Gate classifies (with fallback chain): SKIP → stop, COLD/ACTIVE/PERSIST → continue
          2. Extract summarizes and categorizes
          3. Store persists to SQLite with tier-based TTL
        """
        # Stage 1: Gate (with fallback chain and metrics)
        gate_result, backend_used, fallback_used = self.gate_chain.classify(text)

        if gate_result.decision == GateDecision.SKIP:
            logger.debug(
                f"Gate: SKIP (confidence: {gate_result.confidence:.3f}, backend: {backend_used})"
            )
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
            }

        # Stage 2: Extract
        extraction = self.extractor.extract(
            text, source=source, gate_decision=gate_result.decision.value
        )

        # Override with explicit values if provided
        final_category = category or extraction.category
        final_tier = tier or extraction.tier

        # Stage 2.5: Embed (non-blocking). A failure must NEVER lose the memory —
        # we log and store with a null vector; the dream cycle backfills later.
        # (Whole-content embedding for now; Phase 4 will embed per-chunk.)
        embedding_bytes: Optional[bytes] = None
        embedding_dim: Optional[int] = None
        embedding_model = ""
        try:
            embedding_bytes, embedding_dim, embedding_model = self.embed_chain.embed_text(text)
        except Exception as e:
            logger.warning(f"Embedding failed on capture (storing without vector): {e}")

        # Stage 3: Store
        mem_id = self.store.store(
            content=text,
            summary=extraction.summary,
            category=final_category,
            tier=final_tier,
            key_topics=extraction.key_topics,
            source=source,
            embedding=embedding_bytes,
            embedding_dim=embedding_dim,
            embedding_model=embedding_model,
        )

        # Stage 3.5: Chunk-on-write (non-blocking, §5.4). Long content → several
        # overlapping chunks; short content → one chunk (uniform path). Each chunk
        # is embedded and stored in memory_chunks for chunk-level search (Phase 4.2).
        # Reuse the whole-content embedding when there's a single chunk equal to
        # the embedded text, to avoid a redundant embed on the common short case.
        try:
            chunk_contents = chunk_text(text)
            chunk_rows = []
            for c in chunk_contents:
                if len(chunk_contents) == 1 and c == text and embedding_bytes is not None:
                    c_emb, c_dim, c_model = embedding_bytes, embedding_dim, embedding_model
                else:
                    c_emb, c_dim, c_model = None, None, ""
                    try:
                        c_emb, c_dim, c_model = self.embed_chain.embed_text(c)
                    except Exception as e:
                        logger.warning(f"Chunk embed failed (stored without vector): {e}")
                chunk_rows.append(
                    {
                        "content": c,
                        "embedding": c_emb,
                        "embedding_dim": c_dim,
                        "embedding_model": c_model,
                    }
                )
            self.store_v2.store_chunks(mem_id, chunk_rows)
        except Exception as e:
            logger.warning(f"Chunk-on-write failed (non-blocking): {e}")

        # Stage 4: Graph Wiring (V2)
        # Detect entities and wire them into the knowledge graph
        wiring_result = None
        try:
            wiring_result = self.graph_wiring.wire(text, memory_id=mem_id, source=source)
        except Exception as e:
            logger.warning(f"Graph wiring failed (non-blocking): {e}")

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
            "new_entities": wiring_result.get("new_entities", []) if wiring_result else [],
            "edges_created": len(wiring_result.get("edges", [])) if wiring_result else 0,
        }

    def search(
        self,
        query: str,
        category: Optional[str] = None,
        tier: Optional[str] = None,
        limit: int = 10,
    ) -> list[dict]:
        """Search stored memories by text and metadata filters."""
        return self.store.search(query, category=category, tier=tier, limit=limit)

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
        self, task: str, project: Optional[str] = None, agent: Optional[str] = None, limit: int = 10
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
        return self.graph_traversal.query(entity["id"], depth=depth, edge_types=edge_types)

    def entity_get(self, name: str) -> Optional[dict]:
        """
        Get an entity by name (compiled truth + timeline).
        """
        return self.entity_store.find_entity(name)

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
            "entities": self.entity_store.stats(),
            "facts": self.fact_store.stats(),
            "v2": self.store_v2.v2_stats(),
        }
