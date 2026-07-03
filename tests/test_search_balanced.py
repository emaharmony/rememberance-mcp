"""Phase 3.4 — vector results fused into `_search_balanced` (§5.6).

The vector leg is added as another ranked list into the existing variadic RRF.
To prove the vector leg actually contributes (not just FTS), we store a memory
whose CONTENT shares no words with the query but whose EMBEDDING was computed
from the query text — so only the vector leg can find it.
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
    MemoryStoreV2(v1_store=s)
    return db, s


def _add(store, content, embed_text):
    """Store a memory whose vector is hash(embed_text) (may differ from content)."""
    return store.store(
        content=content,
        summary="s",
        category="misc",
        tier="active",
        key_topics=[],
        embedding=HashEmbedBackend().embed(embed_text),
        embedding_dim=256,
        embedding_model="hash-256",
    )


def test_balanced_surfaces_vector_only_memory():
    with tempfile.TemporaryDirectory() as tmp:
        db, store = _store(tmp)
        # Content shares no words with the query, but its vector == query's vector.
        mid = _add(
            store,
            content="completely unrelated penguins and icebergs",
            embed_text="needlequerytoken",
        )
        search = HybridSearch(db_path=db, entity_store=None)

        # Keyword-only cannot find it (content has none of the query terms).
        assert search.search("needlequerytoken", mode="keyword", limit=5) == []

        # Balanced surfaces it — proof the vector leg is fused in.
        results = search.search("needlequerytoken", mode="balanced", limit=5)
        hit = next((r for r in results if r["id"] == mid), None)
        assert hit is not None, "vector-only memory not surfaced by balanced search"
        assert "vector" in hit["sources"]


def test_balanced_merges_both_sources_when_both_match():
    with tempfile.TemporaryDirectory() as tmp:
        db, store = _store(tmp)
        text = "we decided the canonical event bus owns routing"
        mid = _add(store, content=text, embed_text=text)  # matches both FTS and vector
        search = HybridSearch(db_path=db, entity_store=None)

        results = search.search(text, mode="balanced", limit=5)
        hit = next((r for r in results if r["id"] == mid), None)
        assert hit is not None
        assert "vector" in hit["sources"]
        assert "fts5" in hit["sources"]


def test_balanced_is_fts_only_when_no_vectors():
    # A memory with NO embedding is still found via the FTS leg; balanced must
    # not crash when the vector leg returns nothing for a model.
    with tempfile.TemporaryDirectory() as tmp:
        db, store = _store(tmp)
        mid = store.store(
            content="plain keyword-only memory about turbines",
            summary="s",
            category="misc",
            tier="active",
            key_topics=[],
        )  # no embedding
        search = HybridSearch(db_path=db, entity_store=None)
        results = search.search("turbines", mode="balanced", limit=5)
        hit = next((r for r in results if r["id"] == mid), None)
        assert hit is not None
        assert "fts5" in hit["sources"]
