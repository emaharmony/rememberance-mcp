"""Dream-cycle chunk backfill (`_phase_chunk_backfill`, §5.4/§5.5).

Legacy memories (no chunks) and memories with old-model chunks get
(re)chunked and embedded so chunk-level vector search can find them. Gated
independently by `chunking_enabled` (mirrors `RECALL_CHUNKING_ENABLED`) so
that enabling embeddings alone never triggers a backfill for installs that
have not opted into chunking.
"""

from __future__ import annotations

import hashlib
import math
import sqlite3
import tempfile
from pathlib import Path

from recall_mcp.dream.cycle import DreamCycle
from recall_mcp.embeddings import EmbeddingResult
from recall_mcp.store.edges import EntityStore
from recall_mcp.store.memory import MemoryStoreV2
from recall_mcp.store.store import MemoryStore


class HashEmbeddingProvider:
    """Deterministic, dependency-free embeddings — see test_chunk_search.py."""

    model = "hash-32"

    def __init__(self, dim: int = 32):
        self.dim = dim

    def embed(self, text: str) -> EmbeddingResult:
        data = text.encode("utf-8")
        out: list[float] = []
        counter = 0
        while len(out) < self.dim:
            digest = hashlib.sha256(data + counter.to_bytes(4, "big")).digest()
            for byte in digest:
                out.append((byte / 127.5) - 1.0)
                if len(out) >= self.dim:
                    break
            counter += 1
        norm = math.sqrt(sum(x * x for x in out)) or 1.0
        vector = [x / norm for x in out]
        return EmbeddingResult(vector, self.model, hashlib.sha256(data).hexdigest())


def _setup(tmp, *, chunking_enabled: bool = True):
    db = Path(tmp) / "m.db"
    store = MemoryStore(db_path=db)
    mv2 = MemoryStoreV2(v1_store=store)
    es = EntityStore(db_path=db)
    provider = HashEmbeddingProvider()
    dream = DreamCycle(
        entity_store=es,
        memory_v2=mv2,
        memory_store=store,
        embedding_provider=provider,
        chunking_enabled=chunking_enabled,
    )
    return db, store, mv2, dream, provider


def _chunk_rows(db, mem_id):
    with sqlite3.connect(str(db)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT embedding_model, embedding FROM memory_chunks WHERE memory_id = ?",
            (mem_id,),
        ).fetchall()


def test_backfill_chunks_legacy_memory():
    with tempfile.TemporaryDirectory() as tmp:
        db, store, _mv2, dream, provider = _setup(tmp)
        # Legacy memory: stored directly, so it has NO chunks.
        mid = store.store(
            "a legacy memory with no chunks yet", "s", "misc", "active", []
        )
        assert _chunk_rows(db, mid) == []

        result = dream._phase_chunk_backfill()
        assert result["status"] == "ok"
        assert result["model"] == provider.model
        assert result["memories_chunked"] == 1
        assert result["chunks_written"] >= 1

        rows = _chunk_rows(db, mid)
        assert len(rows) >= 1
        assert all(
            r["embedding_model"] == provider.model and r["embedding"] is not None
            for r in rows
        )


def test_backfill_skips_already_chunked_memory():
    with tempfile.TemporaryDirectory() as tmp:
        db, store, mv2, dream, provider = _setup(tmp)
        mid = store.store("already chunked memory", "s", "misc", "active", [])
        mv2.store_chunks(
            mid,
            [
                {
                    "content": "already chunked memory",
                    "embedding": provider.embed("already chunked memory").to_bytes(),
                    "embedding_dimensions": provider.dim,
                    "embedding_model": provider.model,
                }
            ],
        )
        result = dream._phase_chunk_backfill()
        assert result["stale_found"] == 0
        assert result["memories_chunked"] == 0


def test_backfill_rechunks_wrong_model():
    with tempfile.TemporaryDirectory() as tmp:
        db, store, mv2, dream, provider = _setup(tmp)
        mid = store.store("memory with stale-model chunks", "s", "misc", "active", [])
        mv2.store_chunks(
            mid,
            [
                {
                    "content": "memory with stale-model chunks",
                    "embedding": provider.embed(
                        "memory with stale-model chunks"
                    ).to_bytes(),
                    "embedding_dimensions": provider.dim,
                    "embedding_model": "old-model-v1",
                }
            ],
        )
        result = dream._phase_chunk_backfill()
        assert result["memories_chunked"] == 1
        rows = _chunk_rows(db, mid)
        assert rows  # replaced, still present
        assert all(r["embedding_model"] == provider.model for r in rows)


def test_backfill_dry_run_reports_without_writing():
    with tempfile.TemporaryDirectory() as tmp:
        db, store, _mv2, dream, _provider = _setup(tmp)
        mid = store.store("legacy for dry run", "s", "misc", "active", [])
        result = dream._phase_chunk_backfill(dry_run=True)
        assert result["memories_chunked"] == 0
        assert result["chunks_written"] == 0
        assert result["stale_found"] == 1
        assert _chunk_rows(db, mid) == []  # nothing written


def test_backfill_reachable_via_run():
    with tempfile.TemporaryDirectory() as tmp:
        _db, store, _mv2, dream, _provider = _setup(tmp)
        store.store("legacy via run", "s", "misc", "active", [])
        report = dream.run(phases=["chunk_backfill"])
        phase = next(p for p in report["phases"] if p["phase"] == "chunk_backfill")
        assert phase["status"] == "ok"
        assert phase["details"]["memories_chunked"] == 1


def test_backfill_skipped_without_embedding_provider():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "m.db"
        store = MemoryStore(db_path=db)
        mv2 = MemoryStoreV2(v1_store=store)
        es = EntityStore(db_path=db)
        dream = DreamCycle(
            entity_store=es,
            memory_v2=mv2,
            memory_store=store,
            embedding_provider=None,
            chunking_enabled=True,
        )
        store.store("no provider configured", "s", "misc", "active", [])

        result = dream._phase_chunk_backfill()
        assert result["status"] == "skipped"
        assert result["memories_chunked"] == 0


def test_backfill_skipped_when_chunking_disabled():
    # An embedding provider being present (RECALL_EMBEDDINGS_ENABLED=true)
    # must NOT be enough to trigger a backfill for installs that have not
    # opted into RECALL_CHUNKING_ENABLED.
    with tempfile.TemporaryDirectory() as tmp:
        db, store, _mv2, dream, _provider = _setup(tmp, chunking_enabled=False)
        mid = store.store(
            "legacy memory, chunking opted out", "s", "misc", "active", []
        )

        result = dream._phase_chunk_backfill()
        assert result["status"] == "skipped"
        assert result["memories_chunked"] == 0
        assert _chunk_rows(db, mid) == []
