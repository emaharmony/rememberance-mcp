"""Phase 3.2 — model-matched vector search (`_search_vector`).

Uses the offline hash backend: identical text → identical vector (cosine 1.0),
so "closest seeded memory" == exact-text match. Also covers the provider-
agnostic rule (only compare within one embedding model) and keyword fallback.
"""

import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

from remembrance_mcp.config import Settings
from remembrance_mcp.embed.embed import HashEmbedBackend, vector_to_bytes
from remembrance_mcp.extract import StubExtractor
from remembrance_mcp.gate.backends import GateFallbackChain, HeuristicBackend
from remembrance_mcp.pipeline import MemoryPipeline
from remembrance_mcp.search.hybrid import HybridSearch
from remembrance_mcp.store.memory import MemoryStoreV2
from remembrance_mcp.store.store import MemoryStore


@pytest.fixture
def pipeline():
    with tempfile.TemporaryDirectory() as tmpdir:
        settings = Settings(DB_PATH=Path(tmpdir) / "memories.db", GATE_MODEL_PATH=None)
        pipe = MemoryPipeline(settings=settings)
        pipe.gate_chain = GateFallbackChain([HeuristicBackend()])
        pipe.extractor = StubExtractor()
        yield pipe


def _fresh_store(tmpdir):
    db = Path(tmpdir) / "m.db"
    store = MemoryStore(db_path=db)
    MemoryStoreV2(v1_store=store)  # adds FTS + embedding columns
    return db, store


def test_vector_search_finds_exact_seeded_memory(pipeline):
    a = pipeline.capture("We decided the Alpha subsystem owns routing.", source="test")
    b = pipeline.capture("We decided the Beta module owns persistence.", source="test")
    assert a["id"] and b["id"]

    # Query with A's exact text → A's hash vector matches itself (cosine 1.0).
    results = pipeline.hybrid_search.search(
        "We decided the Alpha subsystem owns routing.", mode="vector", limit=5
    )
    assert results
    assert results[0]["id"] == a["id"]
    assert "vector" in results[0].get("sources", [])


def test_model_matching_ignores_other_model_rows():
    with tempfile.TemporaryDirectory() as tmpdir:
        db, store = _fresh_store(tmpdir)
        target_bytes = HashEmbedBackend().embed("the canonical event bus")
        store.store(
            content="the canonical event bus",
            summary="s",
            category="decision",
            tier="active",
            key_topics=[],
            embedding=target_bytes,
            embedding_dim=256,
            embedding_model="hash-256",
        )
        # A different-model, different-dimension row that must be ignored.
        store.store(
            content="unrelated row in another vector space",
            summary="s",
            category="decision",
            tier="active",
            key_topics=[],
            embedding=vector_to_bytes([0.1, 0.2, 0.3, 0.4]),
            embedding_dim=4,
            embedding_model="other-4",
        )

        search = HybridSearch(db_path=db, entity_store=None)
        results = search.search_with_embedding(target_bytes, model="hash-256")
        assert len(results) == 1
        assert results[0]["content"] == "the canonical event bus"


def test_vector_falls_back_to_keyword_when_no_matching_model():
    with tempfile.TemporaryDirectory() as tmpdir:
        db, store = _fresh_store(tmpdir)
        # Only an other-model row exists; the query embeds to hash-256, which has
        # zero matching rows → _search_vector must fall back to keyword search.
        store.store(
            content="waffles about zebras and quasars",
            summary="s",
            category="misc",
            tier="active",
            key_topics=[],
            embedding=vector_to_bytes([0.1, 0.2, 0.3, 0.4]),
            embedding_dim=4,
            embedding_model="other-4",
        )
        search = HybridSearch(db_path=db, entity_store=None)
        results = search._search_vector("quasars", category=None, limit=5)
        assert any("quasars" in r["content"] for r in results)


def test_vector_search_empty_store_is_safe(pipeline):
    # No memories yet → no crash, keyword fallback returns nothing.
    results = pipeline.hybrid_search.search("anything at all", mode="vector", limit=5)
    assert results == []


_INSERT = (
    "INSERT INTO memories (id, content, summary, category, tier, key_topics, source, "
    "embedding, embedding_dim, embedding_model, created_at, accessed_at, expires_at) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def test_no_cap_regression_target_accessed_last():
    # §7/§8.7: with the old `ORDER BY accessed_at DESC LIMIT 500`, a best-match
    # memory that was accessed long ago is cut before scoring. After the fix it
    # is still retrieved. Target = exact-text (cosine 1.0) but OLDEST accessed_at,
    # buried under 600 more-recently-accessed fillers.
    with tempfile.TemporaryDirectory() as tmpdir:
        db, _store = _fresh_store(tmpdir)
        hb = HashEmbedBackend()
        target_text = "zzz_unique_target_needle_phrase"
        target_vec = hb.embed(target_text)
        now = time.time()

        # NOTE: `with sqlite3.connect()` commits but does NOT close — on Windows
        # the open handle would block tempdir cleanup. Close explicitly.
        conn = sqlite3.connect(str(db))
        try:
            # Target: oldest accessed_at, never-expiring.
            conn.execute(
                _INSERT,
                (
                    "mem_target",
                    target_text,
                    "s",
                    "misc",
                    "active",
                    "[]",
                    "test",
                    target_vec,
                    256,
                    "hash-256",
                    now,
                    now - 100_000,
                    None,
                ),
            )
            fillers = [
                (
                    f"mem_f{i}",
                    f"filler content number {i}",
                    "s",
                    "misc",
                    "active",
                    "[]",
                    "test",
                    hb.embed(f"filler content number {i}"),
                    256,
                    "hash-256",
                    now,
                    now,
                    None,
                )
                for i in range(600)
            ]
            conn.executemany(_INSERT, fillers)
            conn.commit()
        finally:
            conn.close()

        search = HybridSearch(db_path=db, entity_store=None)
        results = search.search_with_embedding(target_vec, model="hash-256", limit=5)
        ids = [r["id"] for r in results]
        assert "mem_target" in ids, "best match dropped by a candidate cap"
        assert results[0]["id"] == "mem_target"  # exact match ranks first
