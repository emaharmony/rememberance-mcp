"""Phase 3.4 + 4.2 — vector results fused into `_search_balanced` (§5.6),
now matched at the CHUNK level and resolved to parent memories (§5.4).

To prove the vector leg contributes (not just FTS), we store a memory whose
CONTENT shares no words with the query but whose CHUNK embedding was computed
from the query text — so only the chunk-level vector leg can find it.
"""

import tempfile
from pathlib import Path

from remembrance_mcp.embed.embed import HashEmbedBackend
from remembrance_mcp.search.hybrid import HybridSearch
from remembrance_mcp.store.memory import MemoryStoreV2
from remembrance_mcp.store.store import MemoryStore


def _store(tmp):
    db = Path(tmp) / "m.db"
    s = MemoryStore(db_path=db)
    mv2 = MemoryStoreV2(v1_store=s)
    return db, s, mv2


def _add(store, mv2, content, chunk_text_for_vector):
    """Store a memory + one chunk whose vector is hash(chunk_text_for_vector).

    ``content`` drives FTS; the chunk vector drives semantic match — they can
    differ, which is exactly how we isolate the vector leg.
    """
    mid = store.store(content=content, summary="s", category="misc", tier="active", key_topics=[])
    mv2.store_chunks(
        mid,
        [
            {
                "content": chunk_text_for_vector,
                "embedding": HashEmbedBackend().embed(chunk_text_for_vector),
                "embedding_dim": 256,
                "embedding_model": "hash-256",
            }
        ],
    )
    return mid


def test_balanced_surfaces_vector_only_memory():
    with tempfile.TemporaryDirectory() as tmp:
        db, store, mv2 = _store(tmp)
        # Content shares no words with the query, but its chunk vector matches it.
        mid = _add(
            store,
            mv2,
            content="completely unrelated penguins and icebergs",
            chunk_text_for_vector="needlequerytoken",
        )
        search = HybridSearch(db_path=db, entity_store=None)

        # Keyword-only cannot find it (content has none of the query terms).
        assert search.search("needlequerytoken", mode="keyword", limit=5) == []

        # Balanced surfaces it — proof the chunk vector leg is fused in.
        results = search.search("needlequerytoken", mode="balanced", limit=5)
        hit = next((r for r in results if r["id"] == mid), None)
        assert hit is not None, "vector-only memory not surfaced by balanced search"
        assert "vector" in hit["sources"]


def test_balanced_merges_both_sources_when_both_match():
    with tempfile.TemporaryDirectory() as tmp:
        db, store, mv2 = _store(tmp)
        text = "we decided the canonical event bus owns routing"
        mid = _add(store, mv2, content=text, chunk_text_for_vector=text)  # FTS + vector
        search = HybridSearch(db_path=db, entity_store=None)

        results = search.search(text, mode="balanced", limit=5)
        hit = next((r for r in results if r["id"] == mid), None)
        assert hit is not None
        assert "vector" in hit["sources"]
        assert "fts5" in hit["sources"]


def test_balanced_is_fts_only_when_no_vectors():
    # A memory with NO chunks/embedding is still found via the FTS leg; balanced
    # must not crash when the chunk vector leg returns nothing.
    with tempfile.TemporaryDirectory() as tmp:
        db, store, _mv2 = _store(tmp)
        mid = store.store(
            content="plain keyword-only memory about turbines",
            summary="s",
            category="misc",
            tier="active",
            key_topics=[],
        )  # no embedding, no chunks
        search = HybridSearch(db_path=db, entity_store=None)
        results = search.search("turbines", mode="balanced", limit=5)
        hit = next((r for r in results if r["id"] == mid), None)
        assert hit is not None
        assert "fts5" in hit["sources"]
