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
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Optional

from recall_mcp.config import Settings
from recall_mcp.cag import CAGDeliveryService, CAGRequest
from recall_mcp.chunking import chunk_text
from recall_mcp.continuity import (
    ContinuityError,
    ContinuityStore,
    SessionService,
    TaskService,
)
from recall_mcp.context import ContextPackRequest, ContextPackService
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
from recall_mcp.feedback import RetrievalFeedbackService
from recall_mcp.gate_backends import GateMetrics
from recall_mcp.handoff import HandoffService
from recall_mcp.outbox import CaptureOutcome, CaptureOutboxDispatcher
from recall_mcp.skills import SkillCandidateService, SkillService
from recall_mcp.store.store import OutboxJob

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

    def __init__(
        self,
        settings: Optional[Settings] = None,
        *,
        start_outbox_worker: bool = True,
    ):
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
            # Guard: refuse to start if existing DB has embeddings from a
            # different model than the configured one. Mixing embedding models
            # produces silently wrong search results.
            try:
                import sqlite3 as _sqlite3
                _conn = _sqlite3.connect(str(self.settings.DB_PATH))
                _existing = _conn.execute(
                    "SELECT DISTINCT embedding_model FROM memories "
                    "WHERE embedding_model IS NOT NULL AND embedding_model != ''"
                ).fetchall()
                _conn.close()
                _models = {row[0] for row in _existing}
                if _models and self.settings.EMBED_MODEL not in _models:
                    raise RuntimeError(
                        f"Embedding model mismatch: DB has embeddings from "
                        f"{', '.join(sorted(_models))} but config has "
                        f"'{self.settings.EMBED_MODEL}'. "
                        f"Set RECALL_EMBED_MODEL to match, or re-embed from scratch."
                    )
            except RuntimeError:
                raise
            except Exception:
                pass  # DB might not exist yet (fresh install)
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
        self.continuity_store = ContinuityStore(self.settings.DB_PATH)
        self.task_service = TaskService(self.continuity_store)
        self.session_service = SessionService(self.continuity_store, self.task_service)
        self.feedback_service = RetrievalFeedbackService(
            self.settings.DB_PATH, self.settings
        )
        self.skill_service = SkillService(self.settings.DB_PATH, self.settings)
        self.skill_candidate_service = SkillCandidateService(self.skill_service)

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
        self.context_service = ContextPackService(
            self.settings.DB_PATH,
            self.settings,
            self.task_service,
            self.session_service,
            self.feedback_service,
            self._search_with_telemetry,
            skill_service=self.skill_service,
        )
        self.handoff_service = HandoffService(
            self.settings.DB_PATH,
            self.settings,
            self.task_service,
            self.session_service,
            self.context_service,
            self.skill_service,
        )
        self.cag_service = CAGDeliveryService(
            self.settings.DB_PATH,
            self.settings,
            self.task_service,
            self.session_service,
            self.context_service,
            self.skill_service,
            self.handoff_service,
        )

        # ── V2: Dream Cycle ──────────────────────────────────
        self.dream_cycle = DreamCycle(
            entity_store=self.entity_store,
            memory_v2=self.store_v2,
            memory_store=self.store,
            embedding_provider=self.embedding_provider,
            ollama_base_url=self.settings.OLLAMA_BASE_URL,
            fact_store=self.fact_store,
            chunking_enabled=self.settings.CHUNKING_ENABLED,
        )

        # ── V2: Markdown Sync ────────────────────────────────
        self.markdown_sync = MarkdownSync(
            self.entity_store, brain_dir=self.settings.BASE_DIR / "brain"
        )

        self._closed = False
        self.outbox_dispatcher = CaptureOutboxDispatcher(
            store=self.store,
            handler=self._finish_capture,
            executor=self._executor,
            capacity=self._processing_slots,
            poll_interval=self.settings.OUTBOX_POLL_INTERVAL,
            lease_seconds=self.settings.OUTBOX_LEASE_SECONDS,
            max_attempts=self.settings.OUTBOX_MAX_ATTEMPTS,
            retry_base_seconds=self.settings.OUTBOX_RETRY_BASE_SECONDS,
        )
        if start_outbox_worker:
            self.outbox_dispatcher.start()

    def close(self, *, wait: bool = True) -> None:
        """Stop durable dispatch and release background processing threads."""
        if self._closed:
            return
        self._closed = True
        self.outbox_dispatcher.stop()
        self._executor.shutdown(wait=wait)

    def __enter__(self) -> "MemoryPipeline":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def capture(
        self,
        text: str,
        source: str = "cli",
        category: Optional[str] = None,
        tier: Optional[str] = None,
        project: Optional[str] = None,
        agent: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
        repository_id: Optional[str] = None,
        task_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> dict:
        """Durably enqueue a capture and wait briefly for derived processing."""
        if self._closed:
            raise RuntimeError("memory pipeline is closed")
        formal_scope = any(
            value is not None
            for value in (
                user_id,
                workspace_id,
                project_id,
                repository_id,
                task_id,
                session_id,
            )
        )
        if formal_scope and task_id is None:
            raise ContinuityError("task_id is required for formally scoped capture")
        if task_id is not None:
            scoped_task = self.task_service.get_task(
                task_id,
                user_id=user_id,
                project_id=project_id,
                repository_id=repository_id,
            )
            if workspace_id is not None and workspace_id != scoped_task["workspace_id"]:
                raise ContinuityError(
                    "task is outside the requested scope", code="not_found"
                )
            user_id = scoped_task["user_id"]
            workspace_id = scoped_task["workspace_id"]
            project_id = scoped_task["project_id"]
            repository_id = scoped_task["repository_id"]
            if session_id is not None:
                scoped_session = self.session_service.get_session(session_id)
                if scoped_session["task_id"] != task_id:
                    raise ContinuityError(
                        "session is outside the requested task", code="scope_mismatch"
                    )
        capture_id, _job_id = self.store.enqueue_capture(
            text,
            source=source,
            project=project or "",
            agent=agent or "",
            user_id=user_id,
            workspace_id=workspace_id,
            project_id=project_id,
            repository_id=repository_id,
            task_id=task_id,
            session_id=session_id,
            category=category,
            tier=tier,
        )
        waiter = self.outbox_dispatcher.register_waiter(capture_id)
        self.outbox_dispatcher.notify()
        try:
            return waiter.result(timeout=self.settings.CAPTURE_PROCESSING_TIMEOUT)
        except FutureTimeoutError:
            self.outbox_dispatcher.abandon_waiter(capture_id, waiter)
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

    def _finish_capture(self, job: OutboxJob) -> CaptureOutcome:
        """Idempotently process one leased capture outbox job."""
        if job.gate_decision is None:
            gate_result, backend_used, fallback_used = self.gate_chain.classify(
                job.content
            )
            decision = gate_result.decision
            confidence = gate_result.confidence
            persisted = self.store.record_gate_result(
                job,
                gate_decision=decision.value,
                gate_confidence=confidence,
                gate_backend=backend_used,
                gate_fallback_used=fallback_used,
            )
            decision = GateDecision(persisted[0])
            confidence, backend_used, fallback_used = persisted[1:]
        else:
            decision = GateDecision(job.gate_decision)
            confidence = float(job.gate_confidence or 0.0)
            backend_used = job.gate_backend or "persisted"
            fallback_used = bool(job.gate_fallback_used)

        if decision == GateDecision.SKIP:
            skip_result: dict[str, object] = {
                "id": None,
                "decision": "SKIP",
                "confidence": confidence,
                "backend": backend_used,
                "fallback_used": fallback_used,
                "category": None,
                "tier": None,
                "summary": None,
                "topics": None,
                "processing_status": "complete",
            }
            return CaptureOutcome(
                result=skip_result,
                memory_id=None,
                raw_status="skipped",
                gate_decision=decision.value,
                gate_confidence=confidence,
                gate_backend=backend_used,
                gate_fallback_used=fallback_used,
            )

        provisional_tier = job.requested_tier or decision.value.lower()
        provisional_category = job.requested_category or "project"
        mem_id = self.store.ensure_capture_memory(
            job,
            gate_decision=decision.value,
            gate_confidence=confidence,
            gate_backend=backend_used,
            gate_fallback_used=fallback_used,
            category=provisional_category,
            tier=provisional_tier,
        )

        extraction = self.extractor.extract(
            job.content,
            source=job.source,
            gate_decision=decision.value,
        )
        final_category = job.requested_category or extraction.category
        final_tier = job.requested_tier or extraction.tier
        self.store.update_enrichment(
            mem_id,
            summary=extraction.summary,
            category=final_category,
            tier=final_tier,
            key_topics=extraction.key_topics,
            processing_status="pending",
        )

        wiring_result = self.graph_wiring.wire(
            job.content, memory_id=mem_id, source=job.source
        )
        facts_created = self._capture_facts(
            job.content, source=f"{job.source}:{mem_id}"
        )

        embedding_status = "disabled"
        if self.embedding_provider is not None:
            if not self._embed_memory(mem_id, job.content):
                raise EmbeddingError(f"embedding failed for {mem_id}")
            embedding_status = "complete"
            if self.settings.CHUNKING_ENABLED:
                self._chunk_memory(mem_id, job.content)
        else:
            self.store.update_enrichment(
                mem_id,
                summary=extraction.summary,
                category=final_category,
                tier=final_tier,
                key_topics=extraction.key_topics,
                processing_status="complete",
            )

        result: dict[str, object] = {
            "id": mem_id,
            "decision": decision.value,
            "confidence": confidence,
            "backend": backend_used,
            "fallback_used": fallback_used,
            "category": final_category,
            "tier": final_tier,
            "summary": extraction.summary,
            "topics": extraction.key_topics,
            "entities": wiring_result.get("entities", []),
            "new_entities": wiring_result.get("new_entities", []),
            "edges_created": len(wiring_result.get("edges", [])),
            "processing_status": "complete",
            "embedding_status": embedding_status,
            "facts_created": len(facts_created),
        }
        return CaptureOutcome(
            result=result,
            memory_id=mem_id,
            raw_status="complete",
            gate_decision=decision.value,
            gate_confidence=confidence,
            gate_backend=backend_used,
            gate_fallback_used=fallback_used,
        )

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
                        derivation_key=(
                            f"{source}|{entity['id']}|{claim_key}|{value.lower()}"
                        ),
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

    def _chunk_memory(self, mem_id: str, text: str) -> bool:
        """Split, embed, and persist per-chunk vectors (chunk-on-write, §5.4).

        Unlike whole-memory embedding, a chunking failure never fails the
        outbox job: chunk-level search is an enhancement over the primary
        whole-memory vector already written by `_embed_memory`, so a partial
        or total chunking failure is logged and the memory is left with
        whatever chunks (if any) it already had.
        """
        if self.embedding_provider is None:
            return False
        try:
            contents = chunk_text(text)
        except Exception as exc:
            logger.warning("Chunking failed for %s (non-blocking): %s", mem_id, exc)
            return False

        chunk_rows: list[dict[str, object]] = []
        for content in contents:
            row: dict[str, object] = {"content": content}
            try:
                embedded = self.embedding_provider.embed(content)
                row["embedding"] = embedded.to_bytes()
                row["embedding_model"] = embedded.model
                row["embedding_dimensions"] = embedded.dimensions
            except EmbeddingError as exc:
                logger.warning(
                    "Chunk embed failed for %s (stored without vector): %s", mem_id, exc
                )
            chunk_rows.append(row)

        try:
            self.store_v2.store_chunks(mem_id, chunk_rows)
            return True
        except Exception as exc:
            logger.warning("Chunk store failed for %s (non-blocking): %s", mem_id, exc)
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
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
        repository_id: Optional[str] = None,
        task_id: Optional[str] = None,
        session_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        include_cold: bool = False,
        retrieval_idempotency_key: Optional[str] = None,
    ) -> list[dict]:
        """Search memories with bounded keyword, vector, balanced, or deep retrieval."""
        results, _run_id = self._search_with_telemetry(
            query=query,
            category=category,
            tier=tier,
            limit=limit,
            mode=mode,
            project=project,
            agent=agent,
            user_id=user_id,
            workspace_id=workspace_id,
            project_id=project_id,
            repository_id=repository_id,
            task_id=task_id,
            session_id=session_id,
            agent_id=agent_id,
            include_cold=include_cold,
            retrieval_idempotency_key=retrieval_idempotency_key,
        )
        return results

    def _search_with_telemetry(
        self,
        *,
        query: str,
        category: Optional[str],
        tier: Optional[str],
        limit: int,
        mode: str,
        project: Optional[str],
        agent: Optional[str],
        user_id: Optional[str],
        workspace_id: Optional[str],
        project_id: Optional[str],
        repository_id: Optional[str],
        task_id: Optional[str],
        session_id: Optional[str],
        agent_id: Optional[str],
        include_cold: bool,
        retrieval_idempotency_key: Optional[str],
    ) -> tuple[list[dict], str | None]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        if mode not in {"keyword", "vector", "balanced", "deep"}:
            raise ValueError("unsupported search mode")
        bounded_limit = max(1, min(int(limit), self.settings.MAX_RESULTS))
        started = time.perf_counter()
        results = self.hybrid_search.search(
            query,
            mode=mode,
            category=category,
            tier=tier,
            limit=bounded_limit,
            project=project,
            agent=agent,
            user_id=user_id,
            workspace_id=workspace_id,
            project_id=project_id,
            repository_id=repository_id,
            task_id=task_id,
            session_id=session_id,
            include_cold=include_cold,
        )
        if (
            not self.settings.UTILITY_SHADOW_MODE
            and self.settings.UTILITY_RANKING_WEIGHT > 0
            and results
        ):
            weight = self.settings.UTILITY_RANKING_WEIGHT
            utility = self.feedback_service.current_utility_scores(
                [str(result["id"]) for result in results]
            )
            for result in results:
                base_score = float(result.get("score", 0.0) or 0.0)
                result["base_score"] = base_score
                result["score"] = (
                    base_score * (1.0 - weight)
                    + utility.get(str(result["id"]), 0.0) * weight
                )
            results.sort(key=lambda item: float(item["score"]), reverse=True)
        latency_ms = (time.perf_counter() - started) * 1000.0
        run_id: str | None = None
        try:
            run_id = self.feedback_service.record_retrieval_run(
                query=query,
                mode=mode,
                requested_limit=bounded_limit,
                latency_ms=latency_ms,
                results=results,
                user_id=user_id,
                workspace_id=workspace_id,
                project_id=project_id,
                repository_id=repository_id,
                task_id=task_id,
                session_id=session_id,
                agent_id=agent_id,
                idempotency_key=retrieval_idempotency_key,
            )
        except Exception as exc:
            self.feedback_service.record_error(exc)
            logger.exception("Retrieval telemetry failed")
        return results, run_id

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
        *,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
        repository_id: Optional[str] = None,
        task_id: Optional[str] = None,
        session_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        known_checkpoint_version: Optional[int] = None,
        token_budget: Optional[int] = None,
        include_cold: bool = False,
        retrieval_idempotency_key: Optional[str] = None,
    ) -> dict:
        """Build through Context Pack V2 and adapt the result for V1 callers."""
        pack = self.build_context_v2(
            ContextPackRequest(
                user_id=user_id,
                workspace_id=workspace_id,
                project_id=project_id,
                repository_id=repository_id,
                task_id=task_id,
                session_id=session_id,
                agent_id=agent_id,
                objective=task,
                max_tokens=token_budget,
                known_checkpoint_version=known_checkpoint_version,
                retrieval_limit=limit,
                include_cold=include_cold,
                legacy_project=project if task_id is None else None,
                legacy_agent=agent if task_id is None else None,
                idempotency_key=retrieval_idempotency_key,
                compatibility_mode=True,
            )
        )
        retrieval = pack["retrieval"]
        session = pack.get("session") or {}
        decisions = pack.get("decisions") or {}
        constraints = pack.get("constraints") or {}
        work_state = pack.get("work_state") or {}
        legacy_warnings = [
            str(warning.get("message", warning))
            if isinstance(warning, dict)
            else str(warning)
            for warning in pack.get("warnings", [])
        ]
        if any((user_id, workspace_id, project_id, repository_id, task_id, session_id)):
            legacy_warnings.append(
                "Global graph synthesis is omitted for formally scoped context."
            )
        return {
            **pack,
            "query": task,
            "project": project,
            "agent": agent,
            "memories": retrieval["results"],
            "entities": [],
            "open_threads": [],
            "total_results": len(retrieval["results"]),
            "retrieval_run_id": retrieval["retrieval_run_id"],
            "latest_checkpoint": session.get("checkpoint"),
            "session_delta": session.get("delta"),
            "approved_decisions": [
                item.get("value") for item in decisions.get("approved", [])
            ],
            "critical_constraints": [
                item.get("value") for item in constraints.get("critical", [])
            ],
            "open_blockers": work_state.get("blockers", []),
            "open_questions": work_state.get("open_questions", []),
            "warnings": legacy_warnings,
        }

    def build_context_v2(self, request: ContextPackRequest) -> dict:
        """Build the canonical provider-neutral context response."""
        return self.context_service.build(request)

    def deliver_context(self, request: CAGRequest) -> dict:
        """Deliver full or delta context through the shared CAG service."""
        return self.cag_service.deliver(request)

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
            "outbox_worker": self.outbox_dispatcher.health(),
            "retrieval_feedback": self.feedback_service.telemetry_stats(),
            "context_packs": self.context_service.stats(),
            "skills": self.skill_service.stats(),
            "handoffs": self.handoff_service.stats(),
            "cag": self.cag_service.stats(),
            "entities": self.entity_store.stats(),
            "facts": self.fact_store.stats(),
            "v2": self.store_v2.v2_stats(),
        }
