"""
Dream Cycle — Automated Brain Maintenance

PATTERN: Scheduled Maintenance Cycle (Garbage Collection for Knowledge)
=========================================================================

The dream cycle runs while you sleep (or on demand) to keep the
knowledge graph healthy and growing. It's the difference between
an agent that forgets and one that remembers.

INSPIRED BY gbrain's 14-phase cycle (lint → backlinks → sync →
synthesize → extract → extract_facts → resolve_symbol_edges →
patterns → recompute_emotional_weight → consolidate →
propose/grade/calibrate → embed → orphans → purge).

V1 simplifies to 7 phases — the essential maintenance without
the gbrain-specific features (code intel, emotional weight,
calibration profile, takes grading).

PHASE ORDER (semantically driven — fix data first, then index):
1. entity_sweep    — detect entities in new memories, create stubs
2. backlink_audit  — fix missing cross-links
3. truth_rewrite   — re-synthesize compiled truth for updated entities
4. pattern_detect  — find cross-session themes
5. orphan_detect   — find disconnected entities
6. embed_stale     — re-embed memories whose content changed
7. purge           — hard-delete expired soft-deletes

Each phase is independently testable and independently runnable.
"""

from __future__ import annotations

import json
import sqlite3
import time
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from recall_mcp.store.edges import EntityStore
from recall_mcp.store.memory import MemoryStoreV2
from recall_mcp.store.store import MemoryStore
from recall_mcp.store.facts import FactStore
from recall_mcp.embeddings import EmbeddingError, EmbeddingProvider
from recall_mcp.graph.entity import EntityDetector
from recall_mcp.graph.edges import GraphWiring

logger = logging.getLogger(__name__)


# ── Phase Types ────────────────────────────────────────────────

ALL_PHASES = [
    "entity_sweep",
    "backlink_audit",
    "truth_rewrite",
    "pattern_detect",
    "fact_resolve",
    "orphan_detect",
    "embed_stale",
    "purge",
]


class DreamCycle:
    """
    The brain maintenance cycle. Runs phases in order to keep
    the knowledge graph healthy and growing.

    Usage:
        dream = DreamCycle(entity_store, memory_v2)
        report = dream.run()                    # all phases
        report = dream.run(phases=["entity_sweep", "orphan_detect"])  # specific phases
    """

    def __init__(
        self,
        entity_store: EntityStore,
        memory_v2: MemoryStoreV2,
        ollama_base_url: str = "http://localhost:11434",
        fact_store: FactStore | None = None,
        memory_store: MemoryStore | None = None,
        embedding_provider: EmbeddingProvider | None = None,
    ):
        self.entity_store = entity_store
        self.memory_v2 = memory_v2
        self.fact_store = fact_store
        self.ollama_base_url = ollama_base_url
        self.memory_store = memory_store
        self.embedding_provider = embedding_provider
        self.wiring = GraphWiring(entity_store)
        self.detector = EntityDetector(entity_store=entity_store)

    @contextmanager
    def _connect(self, db_path: Path) -> Iterator[sqlite3.Connection]:
        """Open a connection that commits/rolls back and always closes.

        A bare ``with sqlite3.connect(...) as conn`` only commits or rolls
        back on exit — it does not close the connection. On Windows this
        can leave the file handle open long enough for a caller's
        ``TemporaryDirectory`` cleanup to fail with a PermissionError, so
        every connection opened here is explicitly closed.
        """
        conn = sqlite3.connect(str(db_path))
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def run(self, phases: list[str] | None = None, dry_run: bool = False) -> dict:
        """
        Run the dream cycle.

        Args:
            phases: Which phases to run (None = all)
            dry_run: If True, report what would happen without making changes

        Returns:
            Dream cycle report with per-phase results
        """
        if phases is None:
            phases = ALL_PHASES

        log_id = self.memory_v2.start_dream_log()
        started_at = time.time()

        phase_results = []
        totals = {
            "entities_created": 0,
            "backlinks_fixed": 0,
            "facts_superseded": 0,
            "truth_rewrites": 0,
            "patterns_found": 0,
            "orphans_found": 0,
            "embeddings_refreshed": 0,
            "purged_count": 0,
        }

        status = "ok"

        for phase in phases:
            if phase not in ALL_PHASES:
                logger.warning(f"Unknown dream phase: {phase}")
                continue

            try:
                phase_start = time.time()
                result = self._run_phase(phase, dry_run=dry_run)
                phase_duration = time.time() - phase_start

                phase_results.append(
                    {
                        "phase": phase,
                        "status": result.get("status", "ok"),
                        "duration_ms": int(phase_duration * 1000),
                        "details": result,
                    }
                )

                # Accumulate totals
                for key in totals:
                    if key in result:
                        totals[key] += result[key]

            except Exception as e:
                logger.error(f"Dream phase {phase} failed: {e}")
                phase_results.append(
                    {
                        "phase": phase,
                        "status": "fail",
                        "duration_ms": 0,
                        "details": {"error": str(e)},
                    }
                )
                status = "partial"

        # Complete the dream log
        total_duration = time.time() - started_at
        self.memory_v2.complete_dream_log(
            log_id,
            status=status,
            phases_run=phases,
            totals=totals,
        )

        return {
            "log_id": log_id,
            "status": status,
            "duration_ms": int(total_duration * 1000),
            "phases": phase_results,
            "totals": totals,
        }

    def _run_phase(self, phase: str, dry_run: bool = False) -> dict:
        """Run a single dream phase."""
        if phase == "entity_sweep":
            return self._phase_entity_sweep(dry_run)
        elif phase == "backlink_audit":
            return self._phase_backlink_audit(dry_run)
        elif phase == "truth_rewrite":
            return self._phase_truth_rewrite(dry_run)
        elif phase == "fact_resolve":
            return self._phase_fact_resolve(dry_run)
        elif phase == "pattern_detect":
            return self._phase_pattern_detect(dry_run)
        elif phase == "orphan_detect":
            return self._phase_orphan_detect(dry_run)
        elif phase == "embed_stale":
            return self._phase_embed_stale(dry_run)
        elif phase == "purge":
            return self._phase_purge(dry_run)
        else:
            return {"status": "skipped", "reason": f"unknown phase: {phase}"}

    # ── Phase Implementations ────────────────────────────────────

    def _phase_entity_sweep(self, dry_run: bool = False) -> dict:
        """
        Phase 1: Scan memories without entity links for entity mentions.
        Create entity stubs and wire them into the graph.
        """
        db_path = self.entity_store.db_path
        entities_created = 0
        links_created = 0

        with self._connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            # Find memories that don't have any entity links yet
            rows = conn.execute("""
                SELECT m.* FROM memories m
                LEFT JOIN memory_entities me ON m.id = me.memory_id
                WHERE me.memory_id IS NULL
                AND m.tier IN ('active', 'persist')
                ORDER BY m.created_at DESC
                LIMIT 100
            """).fetchall()

        for row in rows:
            memory = dict(row)
            mem_id = memory["id"]
            text = memory.get("content", "")

            if not text:
                continue

            # Run entity detection + graph wiring
            wiring_result = self.wiring.wire(
                text, memory_id=mem_id, source="dream:entity_sweep"
            )
            entities_created += len(wiring_result.get("new_entities", []))
            links_created += wiring_result.get("links", 0)

        logger.info(
            f"Entity sweep: {len(rows)} memories scanned, {entities_created} new entities, {links_created} links"
        )

        return {
            "status": "ok",
            "memories_scanned": len(rows),
            "entities_created": entities_created,
            "links_created": links_created,
        }

    def _phase_backlink_audit(self, dry_run: bool = False) -> dict:
        """
        Phase 2: Find entity mentions that are missing back-links
        from the entity page to the source memory.
        """
        db_path = self.entity_store.db_path
        backlinks_fixed = 0

        # Check entities with timeline entries that don't reference
        # the memories that mention them
        with self._connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            entities = conn.execute(
                "SELECT * FROM entities WHERE tier IN ('active', 'persist') LIMIT 100"
            ).fetchall()

        for entity_row in entities:
            entity = dict(entity_row)
            entity_id = entity["id"]

            # Get memories linked to this entity
            linked_memories = self.entity_store.get_entity_memories(entity_id, limit=50)

            # Check if timeline mentions each linked memory
            timeline = entity.get("timeline", "") or ""
            for mem in linked_memories:
                mem_id = mem["id"]
                if mem_id not in timeline:
                    # Add back-link
                    if not dry_run:
                        time.strftime("%Y-%m-%d", time.localtime(mem["created_at"]))
                        self.entity_store.add_timeline_entry(
                            entity_id,
                            f"Referenced in memory {mem_id}",
                            source="dream:backlink_audit",
                        )
                    backlinks_fixed += 1

        return {
            "status": "ok",
            "backlinks_fixed": backlinks_fixed,
        }

    def _phase_truth_rewrite(self, dry_run: bool = False) -> dict:
        """
        Phase 3: Re-synthesize compiled truth for PERSIST entities
        with new timeline entries.

        Uses Nemotron-3-nano (local, free) via Ollama for synthesis.
        If Ollama is unavailable, falls back to timeline summarization.
        """
        truth_rewrites = 0

        # Find PERSIST entities that have been updated since last dream
        entities = self.entity_store.list_entities(limit=100)

        for entity in entities:
            if entity.get("tier") != "persist":
                continue

            timeline = entity.get("timeline", "") or ""
            if not timeline:
                continue

            compiled_truth = entity.get("compiled_truth", "") or ""

            # Only rewrite if timeline has new entries not reflected in compiled truth
            # Simple heuristic: if timeline is significantly longer than compiled truth
            if len(timeline) <= len(compiled_truth) + 50:
                continue

            if dry_run:
                truth_rewrites += 1
                continue

            # Try LLM synthesis via Ollama
            new_truth = self._synthesize_truth(entity["name"], timeline)

            if new_truth and new_truth != compiled_truth:
                self.entity_store.update_entity(entity["id"], compiled_truth=new_truth)
                truth_rewrites += 1

        return {
            "status": "ok",
            "truth_rewrites": truth_rewrites,
        }

    def _phase_pattern_detect(self, dry_run: bool = False) -> dict:
        """
        Phase 4: Find cross-session patterns — topics that keep
        surfacing across multiple memories.

        Simple heuristic: count entity mentions. If an entity is
        mentioned in 3+ memories, it's a recurring pattern.
        """
        db_path = self.entity_store.db_path
        patterns_found = 0

        with self._connect(db_path) as conn:
            # Find entities mentioned in many memories
            rows = conn.execute("""
                SELECT entity_id, COUNT(*) as mention_count
                FROM memory_entities
                GROUP BY entity_id
                HAVING mention_count >= 3
                ORDER BY mention_count DESC
            """).fetchall()

            patterns_found = len(rows)

        return {
            "status": "ok",
            "patterns_found": patterns_found,
            "top_patterns": [{"entity": r[0], "mentions": r[1]} for r in rows[:10]],
        }

    def _phase_orphan_detect(self, dry_run: bool = False) -> dict:
        """
        Phase 5: Find entities with zero edges (disconnected from graph).

        Reports them — doesn't delete. Orphans might need manual wiring
        or might be obsolete.
        """
        orphans = self.entity_store.find_orphans(limit=100)
        return {
            "status": "ok",
            "orphans_found": len(orphans),
            "orphan_ids": [o["id"] for o in orphans[:20]],
        }

    def _phase_fact_resolve(self, dry_run: bool = False) -> dict:
        """Resolve imported competing facts by confidence, then recency."""
        if self.fact_store is None:
            return {
                "status": "skipped",
                "facts_superseded": 0,
                "reason": "fact store is unavailable",
            }
        result = self.fact_store.resolve_contradictions(dry_run=dry_run)
        return {
            "status": "ok",
            "facts_superseded": result["facts_superseded"],
            **result,
        }

    def _phase_embed_stale(self, dry_run: bool = False) -> dict:
        """
        Phase 6: Re-embed memories whose content changed but
        embedding is stale.

        The embedding provider refreshes missing, stale-model, and
        content-hash-mismatched vectors in bounded batches.
        """
        if self.memory_store is None or self.embedding_provider is None:
            return {
                "status": "skipped",
                "embeddings_refreshed": 0,
                "reason": "embedding provider is disabled",
            }

        candidates = self.memory_store.stale_embeddings(
            self.embedding_provider.model, limit=100
        )
        if dry_run:
            return {
                "status": "ok",
                "embeddings_refreshed": len(candidates),
                "dry_run": True,
            }

        refreshed = 0
        failed = 0
        for memory in candidates:
            try:
                embedded = self.embedding_provider.embed(memory["content"])
                self.memory_store.set_embedding(
                    memory["id"],
                    embedded.to_bytes(),
                    model=embedded.model,
                    dimensions=embedded.dimensions,
                    content_hash=embedded.content_hash,
                )
                refreshed += 1
            except EmbeddingError as exc:
                self.memory_store.mark_embedding_error(memory["id"], str(exc))
                failed += 1

        return {
            "status": "ok" if failed == 0 else "partial",
            "embeddings_refreshed": refreshed,
            "embeddings_failed": failed,
        }

    def _phase_purge(self, dry_run: bool = False) -> dict:
        """
        Phase 7: Hard-delete soft-deleted items past 72h recovery window.

        This is the final phase — everything else sees the recoverable
        set first, then purge drops what's expired.
        """
        db_path = self.entity_store.db_path
        now = time.time()
        recovery_window = 72 * 3600  # 72 hours
        cutoff = now - recovery_window
        purged = 0

        with self._connect(db_path) as conn:
            # Delete expired memories past recovery window
            cursor = conn.execute(
                "DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at < ?",
                (cutoff,),
            )
            purged += cursor.rowcount

        return {
            "status": "ok",
            "purged_count": purged,
        }

    # ── Helpers ──────────────────────────────────────────────────

    def _synthesize_truth(self, entity_name: str, timeline: str) -> str:
        """
        Synthesize compiled truth from timeline entries using Ollama.

        Falls back to simple timeline truncation if Ollama is unavailable.
        """
        try:
            import urllib.request
            import urllib.error

            prompt = f"""Synthesize a concise compiled truth summary for {entity_name} from these timeline entries.
Output ONLY the summary paragraph, no bullet points, no headers.

Timeline:
{timeline[:2000]}"""

            payload = json.dumps(
                {
                    "model": "nemotron-3-nano:4b",
                    "prompt": prompt,
                    "stream": False,
                }
            ).encode("utf-8")

            req = urllib.request.Request(
                f"{self.ollama_base_url}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
            )

            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))

            return result.get("response", "").strip()

        except Exception as e:
            logger.warning(f"Truth synthesis failed, using fallback: {e}")
            # Fallback: use the first few timeline entries as truth
            lines = timeline.strip().split("\n")
            return " ".join(lines[:3])[:500]
