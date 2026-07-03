"""Phase 4.1b — memory_chunks table + chunk-on-write in capture() (§5.4).

Short memory → a single chunk; long memory → several overlapping chunks; each
chunk embedded and stored. Chunk-on-write is non-blocking.
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from remembrance_mcp.config import Settings
from remembrance_mcp.extract import StubExtractor
from remembrance_mcp.gate.backends import GateFallbackChain, HeuristicBackend
from remembrance_mcp.pipeline import MemoryPipeline

_CHUNK_COLS = {
    "chunk_id",
    "memory_id",
    "chunk_index",
    "content",
    "embedding",
    "embedding_dim",
    "embedding_model",
    "owner_id",
    "scope",
}


@pytest.fixture
def pipeline():
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(DB_PATH=Path(tmp) / "m.db", GATE_MODEL_PATH=None)
        pipe = MemoryPipeline(settings=settings)
        pipe.gate_chain = GateFallbackChain([HeuristicBackend()])
        pipe.extractor = StubExtractor()
        yield pipe


def _chunks(pipe, mem_id):
    with sqlite3.connect(str(pipe.store.db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM memory_chunks WHERE memory_id = ? ORDER BY chunk_index",
            (mem_id,),
        ).fetchall()


def test_migration_creates_memory_chunks_table(pipeline):
    with sqlite3.connect(str(pipeline.store.db_path)) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_chunks)").fetchall()}
    assert _CHUNK_COLS <= cols


def test_short_memory_is_a_single_chunk(pipeline):
    text = "We decided the event bus is canonical."
    r = pipeline.capture(text, source="test")
    assert r["id"]
    chunks = _chunks(pipeline, r["id"])
    assert len(chunks) == 1
    assert chunks[0]["chunk_index"] == 0
    assert chunks[0]["content"] == text
    assert chunks[0]["embedding"] is not None
    assert chunks[0]["embedding_model"] == "hash-256"
    assert chunks[0]["scope"] == "private"  # forward-compat default


def test_long_memory_produces_multiple_embedded_chunks(pipeline, monkeypatch):
    monkeypatch.setenv("REMEMBRANCE_CHUNK_TARGET_TOKENS", "8")
    monkeypatch.setenv("REMEMBRANCE_CHUNK_OVERLAP_TOKENS", "2")
    monkeypatch.setenv("REMEMBRANCE_CHUNK_SINGLE_THRESHOLD_TOKENS", "8")
    long_text = "We decided " + " ".join(f"topic{i:03d}" for i in range(80))
    r = pipeline.capture(long_text, source="test")
    assert r["id"]

    chunks = _chunks(pipeline, r["id"])
    assert len(chunks) > 1
    # Contiguous 0..N-1 indexing.
    assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))
    # Every chunk embedded with the active model.
    assert all(c["embedding"] is not None for c in chunks)
    assert all(c["embedding_model"] == "hash-256" for c in chunks)


def test_chunk_on_write_is_nonblocking(pipeline, monkeypatch):
    # A chunk-store failure must not lose the memory itself.
    def boom(*a, **k):
        raise RuntimeError("chunk store down")

    monkeypatch.setattr(pipeline.store_v2, "store_chunks", boom)
    r = pipeline.capture("We decided resilience matters most here.", source="test")
    assert r["id"]  # parent memory still stored


def test_store_chunks_replaces_not_appends(pipeline):
    r = pipeline.capture("We decided idempotence is important.", source="test")
    mem_id = r["id"]

    n = pipeline.store_v2.store_chunks(mem_id, [{"content": "a"}, {"content": "b"}])
    assert n == 2
    after = _chunks(pipeline, mem_id)
    assert [c["content"] for c in after] == ["a", "b"]  # replaced the original chunk

    # Empty-content entries are skipped.
    n2 = pipeline.store_v2.store_chunks(mem_id, [{"content": "only"}, {"content": ""}])
    assert n2 == 1
    assert [c["content"] for c in _chunks(pipeline, mem_id)] == ["only"]
