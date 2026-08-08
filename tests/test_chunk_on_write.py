"""Chunk-on-write: per-chunk embeddings persisted during capture (§5.4).

Short memory -> a single chunk; long memory -> several overlapping chunks;
each chunk embedded and stored. Chunk-on-write is non-blocking and gated by
`RECALL_CHUNKING_ENABLED` (default off).

Also closes an integration-coverage gap: `tests/test_hybrid_search.py` only
unit-tests the cosine/serialization helpers in isolation. The tests at the
bottom of this file exercise `mode="vector"`/`"balanced"` search end-to-end,
through a real `MemoryPipeline.capture()` call with a real (deterministic)
`EmbeddingProvider` wired in — mirroring what the reference implementation's
`test_search_balanced.py` / `test_search_vector.py` covered before the merge.
"""

from __future__ import annotations

import hashlib
import math
import sqlite3
from pathlib import Path

import pytest

from recall_mcp.config import Settings
from recall_mcp.embeddings import EmbeddingResult
from recall_mcp.extract import StubExtractor
from recall_mcp.gate_backends import GateFallbackChain, HeuristicBackend
from recall_mcp.pipeline import MemoryPipeline

_CHUNK_COLS = {
    "chunk_id",
    "memory_id",
    "chunk_index",
    "content",
    "embedding",
    "embedding_model",
    "embedding_dimensions",
}


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

    def embed_batch(self, texts: list[str]) -> list[EmbeddingResult]:
        return [self.embed(text) for text in texts]


_PIPELINES: list[MemoryPipeline] = []


@pytest.fixture(autouse=True)
def close_test_pipelines():
    yield
    while _PIPELINES:
        _PIPELINES.pop().close()


def _make_pipeline(tmp_path: Path, *, chunking_enabled: bool = True) -> MemoryPipeline:
    settings = Settings(
        DB_PATH=tmp_path / "memory.db",
        BASE_DIR=tmp_path,
        EMBEDDINGS_ENABLED=True,
        CHUNKING_ENABLED=chunking_enabled,
    )
    pipeline = MemoryPipeline(settings)
    _PIPELINES.append(pipeline)
    pipeline.gate_chain = GateFallbackChain([HeuristicBackend()])
    pipeline.extractor = StubExtractor()
    provider = HashEmbeddingProvider()
    pipeline.embedding_provider = provider
    pipeline.hybrid_search.embedding_provider = provider
    pipeline.dream_cycle.embedding_provider = provider
    return pipeline


@pytest.fixture
def pipeline(tmp_path):
    return _make_pipeline(tmp_path)


def _chunks(pipe: MemoryPipeline, mem_id: str):
    with sqlite3.connect(str(pipe.store.db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM memory_chunks WHERE memory_id = ? ORDER BY chunk_index",
            (mem_id,),
        ).fetchall()


def test_migration_creates_memory_chunks_table(pipeline):
    with sqlite3.connect(str(pipeline.store.db_path)) as conn:
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(memory_chunks)").fetchall()
        }
    assert _CHUNK_COLS <= cols


def test_short_memory_is_a_single_chunk(pipeline):
    text = "We decided the event bus is canonical for Recall today."
    result = pipeline.capture(text, source="test")
    assert result["id"]

    chunks = _chunks(pipeline, result["id"])
    assert len(chunks) == 1
    assert chunks[0]["chunk_index"] == 0
    assert chunks[0]["content"] == text
    assert chunks[0]["embedding"] is not None
    assert chunks[0]["embedding_model"] == "hash-32"


def test_long_memory_produces_multiple_embedded_chunks(pipeline, monkeypatch):
    monkeypatch.setenv("RECALL_CHUNK_TARGET_TOKENS", "8")
    monkeypatch.setenv("RECALL_CHUNK_OVERLAP_TOKENS", "2")
    monkeypatch.setenv("RECALL_CHUNK_SINGLE_THRESHOLD_TOKENS", "8")
    long_text = "We decided " + " ".join(f"topic{i:03d}" for i in range(80))
    result = pipeline.capture(long_text, source="test")
    assert result["id"]

    chunks = _chunks(pipeline, result["id"])
    assert len(chunks) > 1
    # Contiguous 0..N-1 indexing.
    assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))
    # Every chunk embedded with the active model.
    assert all(c["embedding"] is not None for c in chunks)
    assert all(c["embedding_model"] == "hash-32" for c in chunks)


def test_chunk_on_write_is_nonblocking(pipeline, monkeypatch):
    # A chunk-store failure must not lose the memory (or its whole-memory
    # embedding, which is written separately, earlier in the same job).
    def boom(*args, **kwargs):
        raise RuntimeError("chunk store down")

    monkeypatch.setattr(pipeline.store_v2, "store_chunks", boom)
    result = pipeline.capture(
        "We decided resilience matters most here today.", source="test"
    )
    assert result["id"]
    assert result["embedding_status"] == "complete"


def test_store_chunks_replaces_not_appends(pipeline):
    result = pipeline.capture(
        "We decided idempotence is important for Recall.", source="test"
    )
    mem_id = result["id"]

    written = pipeline.store_v2.store_chunks(
        mem_id, [{"content": "a"}, {"content": "b"}]
    )
    assert written == 2
    after = _chunks(pipeline, mem_id)
    assert [c["content"] for c in after] == ["a", "b"]  # replaced the original chunk

    # Empty-content entries are skipped.
    written_again = pipeline.store_v2.store_chunks(
        mem_id, [{"content": "only"}, {"content": ""}]
    )
    assert written_again == 1
    assert [c["content"] for c in _chunks(pipeline, mem_id)] == ["only"]


def test_chunking_disabled_by_default(tmp_path):
    settings = Settings(DB_PATH=tmp_path / "memory.db", BASE_DIR=tmp_path)
    assert settings.CHUNKING_ENABLED is False


def test_capture_skips_chunking_when_disabled(tmp_path):
    pipeline = _make_pipeline(tmp_path, chunking_enabled=False)
    result = pipeline.capture(
        "We decided defaults matter for new installs.", source="test"
    )
    assert result["id"]
    assert result["embedding_status"] == "complete"  # whole-memory embedding still runs
    assert _chunks(pipeline, result["id"]) == []  # but no chunks are written


# ── End-to-end vector / balanced search with a real embedder ────────────────


def test_vector_search_finds_exact_seeded_memory_end_to_end(pipeline):
    alpha = pipeline.capture(
        "We decided the Alpha subsystem owns routing today.", source="test"
    )
    beta = pipeline.capture(
        "We decided the Beta module owns persistence today.", source="test"
    )
    assert alpha["id"] and beta["id"]

    results = pipeline.hybrid_search.search(
        "We decided the Alpha subsystem owns routing today.", mode="vector", limit=5
    )
    assert results
    assert results[0]["id"] == alpha["id"]
    assert {"vector", "sqlite-vec"} & set(results[0]["sources"])


def test_balanced_search_end_to_end_merges_keyword_and_vector(pipeline):
    # No trailing period: FTS5's default tokenizer chokes on a query ending
    # in "." (a pre-existing quirk of `_search_keyword`'s sanitization,
    # unrelated to chunking) — avoid it so this test targets the fusion path.
    text = "We decided the canonical event bus owns routing for Recall"
    captured = pipeline.capture(text, source="test")
    assert captured["id"]

    results = pipeline.hybrid_search.search(text, mode="balanced", limit=5)
    hit = next((r for r in results if r["id"] == captured["id"]), None)
    assert hit is not None
    assert "fts5" in hit["sources"]
    assert {"vector", "sqlite-vec"} & set(hit["sources"])


def test_vector_search_empty_store_is_safe(pipeline):
    # No memories yet -> no crash, keyword fallback returns nothing.
    results = pipeline.hybrid_search.search("anything at all", mode="vector", limit=5)
    assert results == []
