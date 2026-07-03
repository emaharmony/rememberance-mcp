"""Phase 4.3 — dream-cycle chunk backfill (`_phase_chunk_backfill`, §5.4/§5.5).

Legacy memories (no chunks) and memories with old-model chunks get (re)chunked
and embedded so chunk-level vector search can find them. Offline hash backend
(current model = "hash-256").
"""

import sqlite3
import tempfile
from pathlib import Path

from remembrance_mcp.dream.cycle import DreamCycle
from remembrance_mcp.embed.embed import HashEmbedBackend
from remembrance_mcp.store.edges import EntityStore
from remembrance_mcp.store.memory import MemoryStoreV2
from remembrance_mcp.store.store import MemoryStore


def _setup(tmp):
    db = Path(tmp) / "m.db"
    store = MemoryStore(db_path=db)
    mv2 = MemoryStoreV2(v1_store=store)
    es = EntityStore(db_path=db)
    dream = DreamCycle(entity_store=es, memory_v2=mv2)  # embed_chain lazy → hash-256
    return db, store, mv2, dream


def _chunk_rows(db, mem_id):
    with sqlite3.connect(str(db)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT embedding_model, embedding FROM memory_chunks WHERE memory_id = ?",
            (mem_id,),
        ).fetchall()


def test_backfill_chunks_legacy_memory():
    with tempfile.TemporaryDirectory() as tmp:
        db, store, _mv2, dream = _setup(tmp)
        # Legacy memory: stored directly, so it has NO chunks.
        mid = store.store("a legacy memory with no chunks yet", "s", "misc", "active", [])
        assert _chunk_rows(db, mid) == []

        result = dream._phase_chunk_backfill()
        assert result["status"] == "ok"
        assert result["model"] == "hash-256"
        assert result["memories_chunked"] == 1
        assert result["chunks_written"] >= 1

        rows = _chunk_rows(db, mid)
        assert len(rows) >= 1
        assert all(r["embedding_model"] == "hash-256" and r["embedding"] is not None for r in rows)


def test_backfill_skips_already_chunked_memory():
    with tempfile.TemporaryDirectory() as tmp:
        db, store, mv2, dream = _setup(tmp)
        mid = store.store("already chunked memory", "s", "misc", "active", [])
        mv2.store_chunks(
            mid,
            [
                {
                    "content": "already chunked memory",
                    "embedding": HashEmbedBackend().embed("already chunked memory"),
                    "embedding_dim": 256,
                    "embedding_model": "hash-256",
                }
            ],
        )
        result = dream._phase_chunk_backfill()
        assert result["stale_found"] == 0
        assert result["memories_chunked"] == 0


def test_backfill_rechunks_wrong_model():
    with tempfile.TemporaryDirectory() as tmp:
        db, store, mv2, dream = _setup(tmp)
        mid = store.store("memory with stale-model chunks", "s", "misc", "active", [])
        mv2.store_chunks(
            mid,
            [
                {
                    "content": "memory with stale-model chunks",
                    "embedding": HashEmbedBackend().embed("memory with stale-model chunks"),
                    "embedding_dim": 256,
                    "embedding_model": "old-model-v1",
                }
            ],
        )
        result = dream._phase_chunk_backfill()
        assert result["memories_chunked"] == 1
        rows = _chunk_rows(db, mid)
        assert rows  # replaced, still present
        assert all(r["embedding_model"] == "hash-256" for r in rows)


def test_backfill_dry_run_reports_without_writing():
    with tempfile.TemporaryDirectory() as tmp:
        db, store, _mv2, dream = _setup(tmp)
        mid = store.store("legacy for dry run", "s", "misc", "active", [])
        result = dream._phase_chunk_backfill(dry_run=True)
        assert result["memories_chunked"] == 0
        assert result["chunks_written"] == 0
        assert result["stale_found"] == 1
        assert _chunk_rows(db, mid) == []  # nothing written


def test_backfill_reachable_via_run():
    with tempfile.TemporaryDirectory() as tmp:
        _db, store, _mv2, dream = _setup(tmp)
        store.store("legacy via run", "s", "misc", "active", [])
        report = dream.run(phases=["chunk_backfill"])
        phase = next(p for p in report["phases"] if p["phase"] == "chunk_backfill")
        assert phase["status"] == "ok"
        assert phase["details"]["memories_chunked"] == 1
