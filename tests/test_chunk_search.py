"""Chunk-level vector search resolved to parent memories (§5.4/§5.5).

`search_chunks_with_embedding` cosine-ranks `memory_chunks`, dedups to the
best-scoring chunk per parent memory, and returns parent rows. The higher
`_search_vector` / `_search_balanced` levels additively merge those chunk
hits alongside the existing whole-memory embedding path, so a memory that is
only findable through its chunk vector (not through a whole-memory
embedding) is still surfaced by `search(mode="vector"|"balanced")` — this is
the end-to-end coverage that a pure `search_chunks_with_embedding` unit test
cannot exercise on its own.
"""

from __future__ import annotations

import hashlib
import math
import sqlite3
import tempfile
from pathlib import Path

from recall_mcp.embeddings import EmbeddingResult
from recall_mcp.search.hybrid import HybridSearch
from recall_mcp.store.memory import MemoryStoreV2
from recall_mcp.store.store import MemoryStore


class HashEmbeddingProvider:
    """Deterministic, dependency-free embeddings for offline test correctness.

    Same text -> identical vector (cosine 1.0); different text -> a
    different vector. NO semantic meaning — purely a correctness scaffold,
    same role `HashEmbedBackend` played in the reference implementation.
    """

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


_provider = HashEmbeddingProvider()


def _vec(text: str) -> bytes:
    return _provider.embed(text).to_bytes()


def _setup(tmp):
    db = Path(tmp) / "m.db"
    store = MemoryStore(db_path=db)
    mv2 = MemoryStoreV2(v1_store=store)
    return db, store, mv2


def _chunk(text: str, model: str = "hash-32") -> dict:
    return {
        "content": text,
        "embedding": _vec(text),
        "embedding_dimensions": _provider.dim,
        "embedding_model": model,
    }


# ── search_chunks_with_embedding: direct unit coverage ──────────────────────


def test_best_chunk_wins_and_dedups_to_parent():
    with tempfile.TemporaryDirectory() as tmp:
        db, store, mv2 = _setup(tmp)
        mid = store.store("parent memory content", "s", "misc", "active", [])
        # Two chunks: one far from the query, one exactly matching it.
        mv2.store_chunks(
            mid, [_chunk("totally different alpha"), _chunk("the exact query phrase")]
        )

        search = HybridSearch(db_path=db, entity_store=None)
        results = search.search_chunks_with_embedding(
            _vec("the exact query phrase"), model="hash-32"
        )

        assert len(results) == 1  # dedup: exactly one entry per parent memory
        assert results[0]["id"] == mid
        assert results[0]["score"] > 0.9  # best chunk (cosine 1.0) * active boost
        assert (
            results[0]["content"] == "parent memory content"
        )  # PARENT fields, not chunk
        assert results[0]["sources"] == ["vector"]


def test_model_filter_at_chunk_level():
    with tempfile.TemporaryDirectory() as tmp:
        db, store, mv2 = _setup(tmp)
        m1 = store.store("m1", "s", "misc", "active", [])
        mv2.store_chunks(m1, [_chunk("target")])
        m2 = store.store("m2", "s", "misc", "active", [])
        mv2.store_chunks(m2, [_chunk("target", model="other-model")])

        search = HybridSearch(db_path=db, entity_store=None)
        ids = [
            r["id"]
            for r in search.search_chunks_with_embedding(
                _vec("target"), model="hash-32"
            )
        ]
        assert m1 in ids
        assert m2 not in ids


def test_expired_parent_is_dropped():
    with tempfile.TemporaryDirectory() as tmp:
        db, store, mv2 = _setup(tmp)
        mid = store.store("soon gone", "s", "misc", "active", [])
        mv2.store_chunks(mid, [_chunk("target")])
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("UPDATE memories SET expires_at = ? WHERE id = ?", (1.0, mid))
            conn.commit()
        finally:
            conn.close()

        search = HybridSearch(db_path=db, entity_store=None)
        assert (
            search.search_chunks_with_embedding(_vec("target"), model="hash-32") == []
        )


def test_category_filter_applies_to_parent():
    with tempfile.TemporaryDirectory() as tmp:
        db, store, mv2 = _setup(tmp)
        m1 = store.store("a", "s", "decision", "active", [])
        mv2.store_chunks(m1, [_chunk("target")])
        m2 = store.store("b", "s", "misc", "active", [])
        mv2.store_chunks(m2, [_chunk("target")])

        search = HybridSearch(db_path=db, entity_store=None)
        ids = [
            r["id"]
            for r in search.search_chunks_with_embedding(
                _vec("target"), category="decision", model="hash-32"
            )
        ]
        assert m1 in ids
        assert m2 not in ids


def test_empty_when_no_chunks():
    with tempfile.TemporaryDirectory() as tmp:
        db, store, _mv2 = _setup(tmp)
        store.store("no chunks here", "s", "misc", "active", [])
        search = HybridSearch(db_path=db, entity_store=None)
        assert (
            search.search_chunks_with_embedding(_vec("target"), model="hash-32") == []
        )


# ── Merge into `_search_vector` / `_search_balanced` (integration gap) ──────


def test_search_vector_surfaces_chunk_only_memory():
    """A memory with no whole-memory embedding is still found via mode='vector'
    once it has a matching chunk — proving `_search_vector` actually merges
    chunk results in, not just the standalone `search_chunks_with_embedding`.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db, store, mv2 = _setup(tmp)
        mid = store.store(
            content="completely unrelated penguins and icebergs",
            summary="s",
            category="misc",
            tier="active",
            key_topics=[],
        )  # embedding=None: the whole-memory vector leg has nothing to find
        mv2.store_chunks(mid, [_chunk("needlequerytoken")])

        search = HybridSearch(
            db_path=db, entity_store=None, embedding_provider=_provider
        )
        results = search.search("needlequerytoken", mode="vector", limit=5)

        hit = next((r for r in results if r["id"] == mid), None)
        assert hit is not None, "chunk-only memory not surfaced by mode='vector'"
        assert "vector" in hit["sources"]


def test_search_balanced_surfaces_chunk_only_memory():
    """Same scenario, through the default balanced (FTS + vector + RRF) mode."""
    with tempfile.TemporaryDirectory() as tmp:
        db, store, mv2 = _setup(tmp)
        mid = store.store(
            content="completely unrelated penguins and icebergs",
            summary="s",
            category="misc",
            tier="active",
            key_topics=[],
        )
        mv2.store_chunks(mid, [_chunk("needlequerytoken")])
        search = HybridSearch(
            db_path=db, entity_store=None, embedding_provider=_provider
        )

        # Keyword-only cannot find it (content shares no words with the query).
        assert search.search("needlequerytoken", mode="keyword", limit=5) == []

        results = search.search("needlequerytoken", mode="balanced", limit=5)
        hit = next((r for r in results if r["id"] == mid), None)
        assert hit is not None, "chunk-only memory not surfaced by balanced search"
        assert "vector" in hit["sources"]


def test_search_balanced_merges_both_sources_when_both_match():
    with tempfile.TemporaryDirectory() as tmp:
        db, store, mv2 = _setup(tmp)
        text = "we decided the canonical event bus owns routing"
        mid = store.store(
            content=text, summary="s", category="misc", tier="active", key_topics=[]
        )
        mv2.store_chunks(mid, [_chunk(text)])
        search = HybridSearch(
            db_path=db, entity_store=None, embedding_provider=_provider
        )

        results = search.search(text, mode="balanced", limit=5)
        hit = next((r for r in results if r["id"] == mid), None)
        assert hit is not None
        assert "vector" in hit["sources"]
        assert "fts5" in hit["sources"]


def test_search_balanced_is_unaffected_when_no_chunks_exist():
    """No memories chunked at all (chunking disabled/not backfilled yet):
    balanced search must behave exactly as it did before chunk search
    existed — found via FTS, no crash from the (empty) chunk vector leg.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db, store, _mv2 = _setup(tmp)
        mid = store.store(
            content="plain keyword-only memory about turbines",
            summary="s",
            category="misc",
            tier="active",
            key_topics=[],
        )  # no embedding, no chunks
        search = HybridSearch(
            db_path=db, entity_store=None, embedding_provider=_provider
        )
        results = search.search("turbines", mode="balanced", limit=5)
        hit = next((r for r in results if r["id"] == mid), None)
        assert hit is not None
        assert "fts5" in hit["sources"]
