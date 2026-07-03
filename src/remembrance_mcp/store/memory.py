from __future__ import annotations

"""
Memory Store V2 Extensions — Compiled Truth, Timeline, FTS5, Dream Log

Extends the V1 MemoryStore with V2 columns and tables.
Uses the "mixin" pattern: V2Store wraps V1 MemoryStore and adds
new functionality without modifying the original code.

MIGRATION STRATEGY:
- ALTER TABLE adds new columns (compiled_truth, timeline, dream_count, last_dream_at)
- CREATE TABLE adds new tables (memories_fts, dream_log)
- All changes are backward compatible — V1 API still works
- V2 API is opt-in (new methods, new return fields)
"""

import json
import logging
import sqlite3
import time
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
        """Run V2 schema migration (idempotent)."""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            # Add new columns to memories table
            migrations = [
                (
                    "ALTER TABLE memories ADD COLUMN compiled_truth TEXT DEFAULT ''",
                    "compiled_truth",
                ),
                ("ALTER TABLE memories ADD COLUMN timeline TEXT DEFAULT ''", "timeline"),
                ("ALTER TABLE memories ADD COLUMN dream_count INTEGER DEFAULT 0", "dream_count"),
                ("ALTER TABLE memories ADD COLUMN last_dream_at REAL", "last_dream_at"),
            ]
            for sql, col_name in migrations:
                try:
                    conn.execute(sql)
                    logger.info(f"V2 migration: added column '{col_name}' to memories")
                except sqlite3.OperationalError:
                    pass  # Column already exists

            # Create FTS5 virtual table
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
                logger.info("V2 migration: FTS5 virtual table created")

                # Keep the FTS index in sync with `memories`. This is an
                # external-content FTS5 table (content=memories), which is NOT
                # populated automatically — inserts/updates/deletes on the base
                # table must be mirrored into the index via triggers.
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

                # Repair/populate the index from the base table. `rebuild` is the
                # correct, idempotent way to (re)index an external-content table —
                # COUNT(*) on it reflects the content table, so it can't be used to
                # detect an empty index. Cheap at personal scale; runs once per boot.
                conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
                logger.info("V2 migration: FTS5 triggers installed and index rebuilt")

            except sqlite3.OperationalError as e:
                logger.warning(f"FTS5 not available: {e}. Keyword search will use LIKE fallback.")

            # Create dream_log table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS dream_log (
                    id TEXT PRIMARY KEY,
                    started_at REAL NOT NULL,
                    completed_at REAL,
                    status TEXT NOT NULL,
                    phases_run TEXT,
                    totals TEXT,
                    error TEXT
                )
            """)
            logger.info("V2 migration: dream_log table ready")

    # ── FTS5 Search ─────────────────────────────────────────────

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
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row

            if query:
                try:
                    fts_sql = """
                        SELECT m.* FROM memories m
                        JOIN memories_fts fts ON m.rowid = fts.rowid
                        WHERE memories_fts MATCH ?
                    """
                    params = [query]

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
        with sqlite3.connect(str(self.db_path)) as conn:
            cursor = conn.execute(
                "UPDATE memories SET compiled_truth = ? WHERE id = ?", (compiled_truth, mem_id)
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

        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("UPDATE memories SET timeline = ? WHERE id = ?", (new_timeline, mem_id))
        return True

    def touch_dream(self, mem_id: str) -> bool:
        """Mark a memory as touched by the dream cycle."""
        now = time.time()
        with sqlite3.connect(str(self.db_path)) as conn:
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
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                """
                INSERT INTO dream_log (id, started_at, status, phases_run, totals)
                VALUES (?, ?, 'running', '[]', '{}')
            """,
                (log_id, now),
            )
        return log_id

    def complete_dream_log(
        self, log_id: str, status: str, phases_run: list[str], totals: dict, error: str = ""
    ) -> bool:
        """Complete a dream cycle log entry."""
        now = time.time()
        with sqlite3.connect(str(self.db_path)) as conn:
            cursor = conn.execute(
                """
                UPDATE dream_log
                SET completed_at = ?, status = ?, phases_run = ?, totals = ?, error = ?
                WHERE id = ?
            """,
                (now, status, json.dumps(phases_run), json.dumps(totals), error, log_id),
            )
            return cursor.rowcount > 0

    def get_dream_log(self, log_id: str) -> Optional[dict]:
        """Get a dream cycle log entry."""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM dream_log WHERE id = ?", (log_id,)).fetchone()
            if row:
                d = dict(row)
                d["phases_run"] = json.loads(d.get("phases_run", "[]"))
                d["totals"] = json.loads(d.get("totals", "{}"))
                return d
        return None

    def list_dream_logs(self, limit: int = 10) -> list[dict]:
        """List recent dream cycle logs."""
        with sqlite3.connect(str(self.db_path)) as conn:
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
        with sqlite3.connect(str(self.db_path)) as conn:
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
