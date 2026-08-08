"""
Memory Store V2 Extensions — Compiled Truth, Timeline, FTS5, Dream Log

Wraps the canonical memory store with derived FTS5 indexing and V2 behavior.
Canonical columns and tables are installed by the formal migration runner;
the optional FTS index remains rebuildable derived state.
"""

from __future__ import annotations

import json
import sqlite3
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class MemoryStoreV2:
    """
    V2 extensions for MemoryStore.

    Wraps a V1 MemoryStore and adds:
    - compiled_truth + timeline columns on memories
    - FTS5 full-text search virtual table
    - dream_log table for cycle audit
    - search_fts() for keyword search via FTS5
    - timeline/compiled_truth management methods
    """

    def __init__(self, v1_store):
        """
        Initialize V2 extensions on top of a V1 MemoryStore.

        Args:
            v1_store: An instance of MemoryStore (from store.py)
        """
        self.store = v1_store
        self.db_path = v1_store.db_path
        self._migrate_v2()

    def _migrate_v2(self):
        """Initialize the optional, rebuildable FTS5 index."""
        with self.store._connect() as conn:
            try:
                conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                        content,
                        compiled_truth,
                        summary,
                        key_topics,
                        content=memories,
                        content_rowid=rowid
                    )
                """)
                logger.info("V2 derived FTS5 table created")
                conn.executescript("""
                    CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                        INSERT INTO memories_fts(rowid, content, compiled_truth, summary, key_topics)
                        VALUES (new.rowid, new.content, COALESCE(new.compiled_truth, ''),
                                new.summary, new.key_topics);
                    END;
                    CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                        INSERT INTO memories_fts(memories_fts, rowid, content, compiled_truth, summary, key_topics)
                        VALUES ('delete', old.rowid, old.content, COALESCE(old.compiled_truth, ''),
                                old.summary, old.key_topics);
                    END;
                    CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                        INSERT INTO memories_fts(memories_fts, rowid, content, compiled_truth, summary, key_topics)
                        VALUES ('delete', old.rowid, old.content, COALESCE(old.compiled_truth, ''),
                                old.summary, old.key_topics);
                        INSERT INTO memories_fts(rowid, content, compiled_truth, summary, key_topics)
                        VALUES (new.rowid, new.content, COALESCE(new.compiled_truth, ''),
                                new.summary, new.key_topics);
                    END;
                """)
                conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
                logger.info("V2 derived FTS5 index rebuilt")
            except sqlite3.OperationalError as exc:
                logger.warning(
                    "FTS5 not available: %s. Keyword search will use LIKE fallback.",
                    exc,
                )

    # ── Chunk storage (semantic-retrieval.md §5.4) ─────────────

    def store_chunks(self, memory_id: str, chunks: list[dict]) -> int:
        """Replace the stored chunks for a memory.

        ``chunks`` is an ordered list of dicts with a required ``content``
        key and optional ``embedding`` (bytes), ``embedding_dimensions``
        (int), ``embedding_model`` (str). Existing chunks for ``memory_id``
        are deleted first, so this is idempotent and safe to re-run (e.g.
        from the dream-cycle chunk backfill or a manual re-chunk). Returns
        the number of chunk rows written.
        """
        now = time.time()
        with self.store._connect() as conn:
            conn.execute("DELETE FROM memory_chunks WHERE memory_id = ?", (memory_id,))
            written = 0
            for index, chunk in enumerate(chunks):
                content = chunk.get("content")
                if not content:
                    continue
                conn.execute(
                    """
                    INSERT INTO memory_chunks
                        (chunk_id, memory_id, chunk_index, content,
                         embedding, embedding_model, embedding_dimensions, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"{memory_id}::chunk::{index}",
                        memory_id,
                        index,
                        content,
                        chunk.get("embedding"),
                        chunk.get("embedding_model", ""),
                        chunk.get("embedding_dimensions"),
                        now,
                    ),
                )
                written += 1
        return written

    def stale_chunk_memories(self, model: str, limit: int = 100) -> list[dict]:
        """Return memories lacking any embedded chunk for ``model``.

        Covers legacy memories captured before chunking existed and memories
        left with old-model chunks after a model swap; both are invisible to
        chunk-level vector search until re-chunked.
        """
        with self.store._connect() as conn:
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
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (model, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    # FTS5 search

    def search_fts(
        self,
        query: str,
        category: Optional[str] = None,
        tier: Optional[str] = None,
        limit: int = 10,
    ) -> list[dict]:
        """
        Search memories using FTS5 full-text search.

        FTS5 provides:
        - Fast keyword matching across content, compiled_truth, summary
        - Relevance ranking (bm25)
        - Boolean query syntax (AND, OR, NOT)
        - Phrase matching with quotes

        Falls back to V1 LIKE-based search if FTS5 is unavailable.
        """
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row

            if query:
                try:
                    fts_sql = """
                        SELECT m.* FROM memories m
                        JOIN memories_fts fts ON m.rowid = fts.rowid
                        WHERE memories_fts MATCH ?
                    """
                    params: list[object] = [query]

                    if category:
                        fts_sql += " AND m.category = ?"
                        params.append(category)
                    if tier:
                        fts_sql += " AND m.tier = ?"
                        params.append(tier)

                    now = time.time()
                    fts_sql += " AND (m.expires_at IS NULL OR m.expires_at > ?)"
                    params.append(now)

                    fts_sql += " ORDER BY fts.rank, m.accessed_at DESC LIMIT ?"
                    params.append(limit)

                    rows = conn.execute(fts_sql, params).fetchall()
                    if rows:
                        return [dict(r) for r in rows]
                except Exception:
                    pass  # FTS5 not available or syntax issue

        # Fallback to V1 search
        return self.store.search(query, category=category, tier=tier, limit=limit)

    # ── Compiled Truth + Timeline ──────────────────────────────

    def update_compiled_truth(self, mem_id: str, compiled_truth: str) -> bool:
        """Rewrite the compiled truth for a memory (REWRITE, not append)."""
        with self.store._connect() as conn:
            cursor = conn.execute(
                "UPDATE memories SET compiled_truth = ? WHERE id = ?",
                (compiled_truth, mem_id),
            )
            # Also update FTS5
            try:
                rowid = conn.execute(
                    "SELECT rowid FROM memories WHERE id = ?", (mem_id,)
                ).fetchone()
                if rowid:
                    conn.execute(
                        """
                        UPDATE memories_fts SET compiled_truth = ? WHERE rowid = ?
                    """,
                        (compiled_truth, rowid[0]),
                    )
            except Exception:
                pass
            return cursor.rowcount > 0

    def append_timeline(self, mem_id: str, entry: str, source: str = "") -> bool:
        """
        Append a timeline entry to a memory (APPEND, never edit existing).

        FORMAT: "- **YYYY-MM-DD** | {entry} [Source: {source}]"
        """
        memory = self.store.get(mem_id)
        if not memory:
            return False

        now = time.time()
        date_str = time.strftime("%Y-%m-%d", time.localtime(now))
        formatted = f"- **{date_str}** | {entry}"
        if source:
            formatted += f" [Source: {source}]"

        existing_timeline = memory.get("timeline", "") or ""
        new_timeline = formatted + "\n" + existing_timeline  # newest first

        with self.store._connect() as conn:
            conn.execute(
                "UPDATE memories SET timeline = ? WHERE id = ?", (new_timeline, mem_id)
            )
        return True

    def touch_dream(self, mem_id: str) -> bool:
        """Mark a memory as touched by the dream cycle."""
        now = time.time()
        with self.store._connect() as conn:
            cursor = conn.execute(
                "UPDATE memories SET dream_count = dream_count + 1, last_dream_at = ? WHERE id = ?",
                (now, mem_id),
            )
            return cursor.rowcount > 0

    # ── Dream Log ────────────────────────────────────────────────

    def start_dream_log(self) -> str:
        """Start a new dream cycle log entry. Returns the log ID."""
        now = time.time()
        log_id = f"dream_{int(now)}"
        with self.store._connect() as conn:
            conn.execute(
                """
                INSERT INTO dream_log (id, started_at, status, phases_run, totals)
                VALUES (?, ?, 'running', '[]', '{}')
            """,
                (log_id, now),
            )
        return log_id

    def complete_dream_log(
        self,
        log_id: str,
        status: str,
        phases_run: list[str],
        totals: dict,
        error: str = "",
    ) -> bool:
        """Complete a dream cycle log entry."""
        now = time.time()
        with self.store._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE dream_log
                SET completed_at = ?, status = ?, phases_run = ?, totals = ?, error = ?
                WHERE id = ?
            """,
                (
                    now,
                    status,
                    json.dumps(phases_run),
                    json.dumps(totals),
                    error,
                    log_id,
                ),
            )
            return cursor.rowcount > 0

    def get_dream_log(self, log_id: str) -> Optional[dict]:
        """Get a dream cycle log entry."""
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM dream_log WHERE id = ?", (log_id,)
            ).fetchone()
            if row:
                d = dict(row)
                d["phases_run"] = json.loads(d.get("phases_run", "[]"))
                d["totals"] = json.loads(d.get("totals", "{}"))
                return d
        return None

    def list_dream_logs(self, limit: int = 10) -> list[dict]:
        """List recent dream cycle logs."""
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM dream_log ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
            results = []
            for r in rows:
                d = dict(r)
                d["phases_run"] = json.loads(d.get("phases_run", "[]"))
                d["totals"] = json.loads(d.get("totals", "{}"))
                results.append(d)
            return results

    # ── V2 Stats ─────────────────────────────────────────────────

    def v2_stats(self) -> dict:
        """Get V2-specific statistics."""
        with self.store._connect() as conn:
            total_memories = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            with_truth = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE compiled_truth != ''"
            ).fetchone()[0]
            with_timeline = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE timeline != ''"
            ).fetchone()[0]
            dream_touched = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE dream_count > 0"
            ).fetchone()[0]
            dream_logs = conn.execute("SELECT COUNT(*) FROM dream_log").fetchone()[0]

            fts_available = False
            try:
                conn.execute("SELECT COUNT(*) FROM memories_fts LIMIT 1")
                fts_available = True
            except Exception:
                pass

        return {
            "total_memories": total_memories,
            "memories_with_compiled_truth": with_truth,
            "memories_with_timeline": with_timeline,
            "memories_dream_touched": dream_touched,
            "dream_cycle_runs": dream_logs,
            "fts5_available": fts_available,
        }
