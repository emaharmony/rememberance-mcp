"""Phase 4.2 — chunk-level vector search resolved to parents (§5.4/§5.5).

`search_chunks_with_embedding` cosine-ranks memory_chunks, dedups to the
best-scoring chunk per parent memory, and returns parent rows.
"""

import sqlite3
import tempfile
from pathlib import Path

from remembrance_mcp.embed.embed import HashEmbedBackend
from remembrance_mcp.search.hybrid import HybridSearch
from remembrance_mcp.store.memory import MemoryStoreV2
from remembrance_mcp.store.store import MemoryStore

_hb = HashEmbedBackend()


def _vec(text):
    return _hb.embed(text)


def _setup(tmp):
    db = Path(tmp) / "m.db"
    s = MemoryStore(db_path=db)
    mv2 = MemoryStoreV2(v1_store=s)
    return db, s, mv2


def _chunk(text, model="hash-256"):
    return {
        "content": text,
        "embedding": _vec(text),
        "embedding_dim": 256,
        "embedding_model": model,
    }


def test_best_chunk_wins_and_dedups_to_parent():
    with tempfile.TemporaryDirectory() as tmp:
        db, s, mv2 = _setup(tmp)
        mid = s.store("parent memory content", "s", "misc", "active", [])
        # Two chunks: one far from the query, one exactly matching it.
        mv2.store_chunks(mid, [_chunk("totally different alpha"), _chunk("the exact query phrase")])

        search = HybridSearch(db_path=db, entity_store=None)
        results = search.search_chunks_with_embedding(
            _vec("the exact query phrase"), model="hash-256"
        )

        assert len(results) == 1  # dedup: exactly one entry per parent memory
        assert results[0]["id"] == mid
        assert results[0]["score"] > 0.9  # best chunk (cosine 1.0) * active boost
        assert results[0]["content"] == "parent memory content"  # PARENT fields, not chunk
        assert results[0]["sources"] == ["vector"]


def test_model_filter_at_chunk_level():
    with tempfile.TemporaryDirectory() as tmp:
        db, s, mv2 = _setup(tmp)
        m1 = s.store("m1", "s", "misc", "active", [])
        mv2.store_chunks(m1, [_chunk("target")])
        m2 = s.store("m2", "s", "misc", "active", [])
        mv2.store_chunks(m2, [_chunk("target", model="other-model")])

        search = HybridSearch(db_path=db, entity_store=None)
        ids = [
            r["id"] for r in search.search_chunks_with_embedding(_vec("target"), model="hash-256")
        ]
        assert m1 in ids
        assert m2 not in ids


def test_expired_parent_is_dropped():
    with tempfile.TemporaryDirectory() as tmp:
        db, s, mv2 = _setup(tmp)
        mid = s.store("soon gone", "s", "misc", "active", [])
        mv2.store_chunks(mid, [_chunk("target")])
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("UPDATE memories SET expires_at = ? WHERE id = ?", (1.0, mid))
            conn.commit()
        finally:
            conn.close()

        search = HybridSearch(db_path=db, entity_store=None)
        assert search.search_chunks_with_embedding(_vec("target"), model="hash-256") == []


def test_category_filter_applies_to_parent():
    with tempfile.TemporaryDirectory() as tmp:
        db, s, mv2 = _setup(tmp)
        m1 = s.store("a", "s", "decision", "active", [])
        mv2.store_chunks(m1, [_chunk("target")])
        m2 = s.store("b", "s", "misc", "active", [])
        mv2.store_chunks(m2, [_chunk("target")])

        search = HybridSearch(db_path=db, entity_store=None)
        ids = [
            r["id"]
            for r in search.search_chunks_with_embedding(
                _vec("target"), category="decision", model="hash-256"
            )
        ]
        assert m1 in ids
        assert m2 not in ids


def test_empty_when_no_chunks():
    with tempfile.TemporaryDirectory() as tmp:
        db, s, _mv2 = _setup(tmp)
        s.store("no chunks here", "s", "misc", "active", [])
        search = HybridSearch(db_path=db, entity_store=None)
        assert search.search_chunks_with_embedding(_vec("target"), model="hash-256") == []
