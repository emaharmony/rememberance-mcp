from __future__ import annotations

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

import json
import logging
import sqlite3
import time

from remembrance_mcp.chunk.chunk import chunk_text
from remembrance_mcp.graph.edges import GraphWiring
from remembrance_mcp.graph.entity import EntityDetector
from remembrance_mcp.store.edges import EntityStore
from remembrance_mcp.store.memory import MemoryStoreV2

logger = logging.getLogger(__name__)


# ── Phase Types ────────────────────────────────────────────────

ALL_PHASES = [
    "entity_sweep",
    "backlink_audit",
    "truth_rewrite",
    "pattern_detect",
    "orphan_detect",
    "embed_stale",
    "chunk_backfill",
    "purge",
]

# Max memories re-embedded per dream cycle (semantic-retrieval.md §5.5). A large
# backlog (e.g. after a model swap) drains over several cycles instead of
# blocking one run.
EMBED_BACKFILL_BATCH = 200


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
        embed_chain=None,
    ):
        self.entity_store = entity_store
        self.memory_v2 = memory_v2
        self.ollama_base_url = ollama_base_url
        self.wiring = GraphWiring(entity_store)
        self.detector = EntityDetector(entity_store=entity_store)
        # Embedder for backfill (embed_stale). Lazy so cycles that skip that
        # phase pay nothing; defaults to the env-configured chain.
        self._embed_chain = embed_chain

    def _get_embed_chain(self):
        if self._embed_chain is None:
            from remembrance_mcp.embed.embed import build_embed_chain

            self._embed_chain = build_embed_chain()
        return self._embed_chain

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
        elif phase == "pattern_detect":
            return self._phase_pattern_detect(dry_run)
        elif phase == "orphan_detect":
            return self._phase_orphan_detect(dry_run)
        elif phase == "embed_stale":
            return self._phase_embed_stale(dry_run)
        elif phase == "chunk_backfill":
            return self._phase_chunk_backfill(dry_run)
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

        with sqlite3.connect(str(db_path)) as conn:
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
            wiring_result = self.wiring.wire(text, memory_id=mem_id, source="dream:entity_sweep")
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
        with sqlite3.connect(str(db_path)) as conn:
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
                        date_str = time.strftime("%Y-%m-%d", time.localtime(mem["created_at"]))
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

        with sqlite3.connect(str(db_path)) as conn:
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

    def _phase_embed_stale(self, dry_run: bool = False) -> dict:
        """
        Phase 6: Embedding backfill (semantic-retrieval.md §5.5 "Backfill").

        Re-embeds memories whose vector is missing (never embedded, or an embed
        failed non-blockingly on write) or was produced by a DIFFERENT model
        than the currently-active one — a model swap leaves old vectors in an
        incompatible space, so they must be refreshed before they're searchable
        again. Bounded per run (EMBED_BACKFILL_BATCH) so a large backlog drains
        over several cycles. Returns a TRUE ``embeddings_refreshed`` count.
        """
        db_path = self.entity_store.db_path
        chain = self._get_embed_chain()

        # Probe once to learn the active model, so we can spot wrong-model rows.
        try:
            _, _, current_model = chain.embed_text("probe")
        except Exception as e:
            logger.warning(f"embed_stale: no embedder available ({e}); skipping")
            return {"status": "ok", "embeddings_refreshed": 0, "note": "no embedder"}

        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, content FROM memories
                WHERE content IS NOT NULL AND content != ''
                AND (embedding IS NULL OR embedding_model IS NULL
                     OR embedding_model = '' OR embedding_model != ?)
                LIMIT ?
                """,
                (current_model, EMBED_BACKFILL_BATCH),
            ).fetchall()

            stale_found = len(rows)
            if dry_run:
                return {
                    "status": "ok",
                    "embeddings_refreshed": 0,
                    "stale_found": stale_found,
                    "note": f"{stale_found} row(s) would be re-embedded with {current_model}",
                }

            refreshed = 0
            for r in rows:
                try:
                    vec, dim, model_id = chain.embed_text(r["content"])
                except Exception as e:
                    # Non-blocking: skip this row, keep draining the rest.
                    logger.warning(f"embed_stale: failed to embed {r['id']}: {e}")
                    continue
                conn.execute(
                    "UPDATE memories SET embedding = ?, embedding_dim = ?, "
                    "embedding_model = ? WHERE id = ?",
                    (vec, dim, model_id, r["id"]),
                )
                refreshed += 1

        return {
            "status": "ok",
            "embeddings_refreshed": refreshed,
            "stale_found": stale_found,
            "model": current_model,
        }

    def _phase_chunk_backfill(self, dry_run: bool = False) -> dict:
        """
        Phase: backfill chunks for existing memories (semantic-retrieval.md §5.4/§5.5).

        Chunk-level search only finds memories that have current-model chunks.
        Legacy memories captured before chunking existed — or memories left with
        old-model chunks after a model swap — have none, so they're invisible to
        vector search until re-chunked. This finds memories lacking any current-
        model embedded chunk, re-runs chunk_text → embed → store_chunks (which
        replaces stale chunks), bounded per run. Returns true counts.
        """
        db_path = self.entity_store.db_path
        chain = self._get_embed_chain()

        try:
            _, _, current_model = chain.embed_text("probe")
        except Exception as e:
            logger.warning(f"chunk_backfill: no embedder available ({e}); skipping")
            return {
                "status": "ok",
                "memories_chunked": 0,
                "chunks_written": 0,
                "note": "no embedder",
            }

        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, content FROM memories
                WHERE content IS NOT NULL AND content != ''
                AND NOT EXISTS (
                    SELECT 1 FROM memory_chunks c
                    WHERE c.memory_id = memories.id
                    AND c.embedding_model = ? AND c.embedding IS NOT NULL
                )
                LIMIT ?
                """,
                (current_model, EMBED_BACKFILL_BATCH),
            ).fetchall()

        stale_found = len(rows)
        if dry_run:
            return {
                "status": "ok",
                "memories_chunked": 0,
                "chunks_written": 0,
                "stale_found": stale_found,
                "note": f"{stale_found} memory(ies) would be re-chunked with {current_model}",
            }

        memories_chunked = 0
        chunks_written = 0
        for r in rows:
            try:
                contents = chunk_text(r["content"])
                chunk_rows = []
                for c in contents:
                    emb, dim, model = None, None, ""
                    try:
                        emb, dim, model = chain.embed_text(c)
                    except Exception as e:
                        logger.warning(f"chunk_backfill: embed failed for {r['id']} chunk: {e}")
                    chunk_rows.append(
                        {
                            "content": c,
                            "embedding": emb,
                            "embedding_dim": dim,
                            "embedding_model": model,
                        }
                    )
                chunks_written += self.memory_v2.store_chunks(r["id"], chunk_rows)
                memories_chunked += 1
            except Exception as e:
                # Non-blocking: skip this memory, keep draining the rest.
                logger.warning(f"chunk_backfill: failed for {r['id']}: {e}")
                continue

        return {
            "status": "ok",
            "memories_chunked": memories_chunked,
            "chunks_written": chunks_written,
            "stale_found": stale_found,
            "model": current_model,
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

        with sqlite3.connect(str(db_path)) as conn:
            # Delete expired memories past recovery window
            cursor = conn.execute(
                "DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at < ?", (cutoff,)
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
            import urllib.error
            import urllib.request

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
