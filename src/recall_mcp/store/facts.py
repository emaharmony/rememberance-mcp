"""
Fact Store — Structured Claims with Provenance

PATTERN: Temporal Fact Store (Event Sourcing Light)
=====================================================

Facts are structured claims about entities, each with:
- entity_id: which entity this fact is about
- claim_key: what aspect (e.g., "role", "status", "tech_stack")
- claim_value: the current value (e.g., "lead developer", "active")
- source: where we learned this
- confidence: how sure we are
- observed_at: when we first saw this claim
- superseded_at: when a newer claim replaced this (NULL = current)

WHY STRUCTURED FACTS?
- Two sources disagree → both stored, visible as a conflict
- "Ema is using Go" + "Ema switched to Python" → both visible,
  the dream cycle resolves which is current
- Facts are queryable: "What's Ema's role?" → structured answer
- The compiled truth section is generated from the fact store's
  latest-confident values

INSPIRED BY gbrain's four database primitives:
- Entity registry → our entities table
- Event ledger → our timeline field
- Fact store → THIS table
- Relationship graph → our edges table
"""

from __future__ import annotations

import sqlite3
import time
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from recall_mcp.store.migrations import run_migrations

logger = logging.getLogger(__name__)


@contextmanager
def _connect(db_path: Path):
    """Open a SQLite connection that commits/rolls back and closes on Windows."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


class FactStore:
    """
    SQLite-backed fact store with temporal versioning.

    Each fact is a structured claim about an entity. When new information
    contradicts an existing fact, both are stored — the older one gets
    `superseded_at` set, but is never deleted (audit trail).

    The dream cycle resolves contradictions by examining all current
    (unsuperseded) facts and updating compiled truth accordingly.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_table()

    def _init_table(self):
        """Install the shared canonical schema through the migration runner."""
        run_migrations(self.db_path)
        logger.info("Fact store initialized at %s", self.db_path)

    def assert_fact(
        self,
        entity_id: str,
        claim_key: str,
        claim_value: str,
        source: str,
        confidence: float = 1.0,
        derivation_key: str | None = None,
    ) -> str:
        """Assert a temporal fact, optionally idempotent by derivation key."""
        now = time.time()
        fact_id = f"fact_{int(now * 1000)}_{entity_id}_{claim_key}"

        with _connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if derivation_key:
                existing = conn.execute(
                    "SELECT id FROM facts WHERE derivation_key = ?",
                    (derivation_key,),
                ).fetchone()
                if existing is not None:
                    conn.execute(
                        "UPDATE facts SET confidence = ?, source = ? WHERE id = ?",
                        (confidence, source, existing["id"]),
                    )
                    return str(existing["id"])

            current = conn.execute(
                """
                SELECT * FROM facts
                WHERE entity_id = ? AND claim_key = ? AND superseded_at IS NULL
                ORDER BY observed_at DESC LIMIT 1
                """,
                (entity_id, claim_key),
            ).fetchone()
            if current and current["claim_value"] != claim_value:
                conn.execute(
                    "UPDATE facts SET superseded_at = ? WHERE id = ?",
                    (now, current["id"]),
                )
                logger.info(
                    "Fact superseded: %s.%s = %s -> %s",
                    entity_id,
                    claim_key,
                    current["claim_value"],
                    claim_value,
                )

            try:
                conn.execute(
                    """
                    INSERT INTO facts (
                        id, entity_id, claim_key, claim_value, source,
                        confidence, observed_at, derivation_key
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fact_id,
                        entity_id,
                        claim_key,
                        claim_value,
                        source,
                        confidence,
                        now,
                        derivation_key,
                    ),
                )
            except sqlite3.IntegrityError:
                if derivation_key:
                    existing = conn.execute(
                        "SELECT id FROM facts WHERE derivation_key = ?",
                        (derivation_key,),
                    ).fetchone()
                    if existing is not None:
                        return str(existing["id"])
                raise

        return fact_id

    def get_current_fact(self, entity_id: str, claim_key: str) -> Optional[dict]:
        """Get the current (unsuperseded) fact for an entity + key."""
        with _connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT * FROM facts
                WHERE entity_id = ? AND claim_key = ? AND superseded_at IS NULL
                ORDER BY observed_at DESC LIMIT 1
            """,
                (entity_id, claim_key),
            ).fetchone()
            return dict(row) if row else None

    def get_entity_facts(self, entity_id: str, current_only: bool = True) -> list[dict]:
        """Get all facts for an entity. If current_only, exclude superseded."""
        with _connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            sql = "SELECT * FROM facts WHERE entity_id = ?"
            if current_only:
                sql += " AND superseded_at IS NULL"
            sql += " ORDER BY claim_key, observed_at DESC"
            rows = conn.execute(sql, (entity_id,)).fetchall()
            return [dict(r) for r in rows]

    def get_fact_history(self, entity_id: str, claim_key: str) -> list[dict]:
        """Get the full history of a claim (including superseded)."""
        with _connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM facts
                WHERE entity_id = ? AND claim_key = ?
                ORDER BY observed_at DESC
            """,
                (entity_id, claim_key),
            ).fetchall()
            return [dict(r) for r in rows]

    def find_contradictions(self) -> list[dict]:
        """
        Find entities where multiple unsuperseded facts disagree
        on the same claim_key.

        This is a signal for the dream cycle to resolve.
        """
        with _connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            # Find claim_keys with multiple distinct current values
            rows = conn.execute("""
                SELECT entity_id, claim_key, COUNT(DISTINCT claim_value) as value_count
                FROM facts
                WHERE superseded_at IS NULL
                GROUP BY entity_id, claim_key
                HAVING value_count > 1
            """).fetchall()
            contradictions = []
            for r in rows:
                facts = conn.execute(
                    """
                    SELECT * FROM facts
                    WHERE entity_id = ? AND claim_key = ? AND superseded_at IS NULL
                    ORDER BY confidence DESC, observed_at DESC
                """,
                    (r["entity_id"], r["claim_key"]),
                ).fetchall()
                contradictions.append(
                    {
                        "entity_id": r["entity_id"],
                        "claim_key": r["claim_key"],
                        "conflicting_values": [dict(f) for f in facts],
                    }
                )
            return contradictions

    def resolve_contradictions(self, dry_run: bool = False) -> dict:
        """Keep the highest-confidence newest fact and supersede competing claims."""
        contradictions = self.find_contradictions()
        superseded = 0
        winners: list[str] = []
        if not dry_run:
            now = time.time()
            with _connect(self.db_path) as conn:
                for contradiction in contradictions:
                    facts = contradiction["conflicting_values"]
                    if not facts:
                        continue
                    winners.append(facts[0]["id"])
                    losing_ids = [fact["id"] for fact in facts[1:]]
                    for fact_id in losing_ids:
                        conn.execute(
                            "UPDATE facts SET superseded_at = ? WHERE id = ?",
                            (now, fact_id),
                        )
                    superseded += len(losing_ids)
        else:
            superseded = sum(
                max(0, len(item["conflicting_values"]) - 1) for item in contradictions
            )

        return {
            "contradictions_found": len(contradictions),
            "facts_superseded": superseded,
            "winning_fact_ids": winners,
            "dry_run": dry_run,
        }

    def stats(self) -> dict:
        """Get fact store statistics."""
        with _connect(self.db_path) as conn:
            total = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
            current = conn.execute(
                "SELECT COUNT(*) FROM facts WHERE superseded_at IS NULL"
            ).fetchone()[0]
            superseded = total - current
            return {
                "total_facts": total,
                "current_facts": current,
                "superseded_facts": superseded,
            }
