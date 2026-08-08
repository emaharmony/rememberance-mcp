"""Regression coverage for the dream-cycle purge phase (`_phase_purge`).

Bug: `_phase_purge` hard-deletes expired memories through `DreamCycle._connect`,
which opened a bare `sqlite3.connect(...)` without `PRAGMA foreign_keys=ON`.
`memory_chunks.memory_id` and `memory_entities.memory_id` both declare
`ON DELETE CASCADE` at the schema level, but that clause is only enforced
when the deleting connection has foreign key enforcement turned on — so the
purge silently left every chunk and entity link behind as an orphan
(observed in production: `dream_1785653999` purged 54 memories and left
1,029 orphaned `memory_chunks` rows plus 611 orphaned `memory_entities`
rows). The fix turns on `PRAGMA foreign_keys=ON` in `DreamCycle._connect`,
so the existing cascades actually fire.

Every raw sqlite3 connection opened in this file is wrapped in
`contextlib.closing` (never a bare `with sqlite3.connect(...) as conn:`)
per the same Windows file-handle gotcha `DreamCycle._connect` itself
documents: a bare `with conn:` only commits/rolls back, it does not close
the connection, which leaves a lock behind that breaks tmp_path cleanup.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path

from recall_mcp.dream.cycle import DreamCycle
from recall_mcp.store.edges import EntityStore
from recall_mcp.store.memory import MemoryStoreV2
from recall_mcp.store.store import MemoryStore


def _setup(tmp_path: Path):
    db = tmp_path / "m.db"
    store = MemoryStore(db_path=db)
    mv2 = MemoryStoreV2(v1_store=store)
    es = EntityStore(db_path=db)
    dream = DreamCycle(entity_store=es, memory_v2=mv2, memory_store=store)
    return db, store, mv2, es, dream


def _expire(db: Path, mem_id: str, *, seconds_ago: float) -> None:
    """Force a memory into the purge phase's expired window directly.

    `_phase_purge` only picks up memories whose `expires_at` is more than 72h
    in the past; `MemoryStore.store()` sets `expires_at` from tier-based TTLs,
    so tests set it directly to avoid waiting on real TTLs.
    """
    with closing(sqlite3.connect(db)) as conn:
        with conn:
            conn.execute(
                "UPDATE memories SET expires_at = ? WHERE id = ?",
                (time.time() - seconds_ago, mem_id),
            )


def _count(db: Path, sql: str, params: tuple = ()) -> int:
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute(sql, params).fetchone()[0]


def _foreign_key_check(db: Path) -> list:
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute("PRAGMA foreign_key_check").fetchall()


def test_purge_cascades_memory_chunks_and_entities(tmp_path):
    db, store, mv2, es, dream = _setup(tmp_path)

    mem_id = store.store("memory that will be purged", "s", "misc", "cold", [])
    _expire(db, mem_id, seconds_ago=73 * 3600)

    mv2.store_chunks(
        mem_id,
        [
            {"content": "chunk one", "embedding": None},
            {"content": "chunk two", "embedding": None},
        ],
    )
    entity_id = es.create_entity("Purge Target", "concept")
    assert es.link_memory_entity(mem_id, entity_id) is True

    assert (
        _count(db, "SELECT COUNT(*) FROM memory_chunks WHERE memory_id = ?", (mem_id,))
        == 2
    )
    assert (
        _count(
            db, "SELECT COUNT(*) FROM memory_entities WHERE memory_id = ?", (mem_id,)
        )
        == 1
    )

    result = dream._phase_purge()
    assert result["status"] == "ok"
    assert result["purged_count"] == 1

    assert _count(db, "SELECT COUNT(*) FROM memories WHERE id = ?", (mem_id,)) == 0
    assert _count(db, "SELECT COUNT(*) FROM memory_chunks") == 0
    assert _count(db, "SELECT COUNT(*) FROM memory_entities") == 0
    assert _foreign_key_check(db) == []


def test_purge_leaves_unrelated_live_memory_and_its_dependents_intact(tmp_path):
    db, store, mv2, es, dream = _setup(tmp_path)

    expired_id = store.store("expired memory", "s", "misc", "cold", [])
    _expire(db, expired_id, seconds_ago=73 * 3600)

    live_id = store.store("live memory", "s", "misc", "persist", [])
    mv2.store_chunks(live_id, [{"content": "live chunk", "embedding": None}])
    entity_id = es.create_entity("Live Target", "concept")
    assert es.link_memory_entity(live_id, entity_id) is True

    result = dream._phase_purge()
    assert result["purged_count"] == 1

    assert _count(db, "SELECT COUNT(*) FROM memories WHERE id = ?", (live_id,)) == 1
    assert (
        _count(db, "SELECT COUNT(*) FROM memory_chunks WHERE memory_id = ?", (live_id,))
        == 1
    )
    assert (
        _count(
            db, "SELECT COUNT(*) FROM memory_entities WHERE memory_id = ?", (live_id,)
        )
        == 1
    )
    assert _foreign_key_check(db) == []


def test_purge_phase_reachable_via_run(tmp_path):
    db, store, mv2, es, dream = _setup(tmp_path)

    mem_id = store.store("expired via run()", "s", "misc", "cold", [])
    _expire(db, mem_id, seconds_ago=73 * 3600)
    mv2.store_chunks(mem_id, [{"content": "chunk", "embedding": None}])

    report = dream.run(phases=["purge"])
    phase = next(p for p in report["phases"] if p["phase"] == "purge")
    assert phase["status"] == "ok"
    assert phase["details"]["purged_count"] == 1
    assert report["totals"]["purged_count"] == 1

    assert _count(db, "SELECT COUNT(*) FROM memory_chunks") == 0
    assert _foreign_key_check(db) == []
