"""
Tests for HybridSearch — FTS5 + Vector + Graph + RRF
"""

import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path

import pytest

from remembrance_mcp.search.hybrid import TIER_BOOST, HybridSearch
from remembrance_mcp.store.edges import EntityStore


@pytest.fixture
def search_env():
    """Set up a test database with memories, entities, and search."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_search.db"
        entity_db = Path(tmpdir) / "test_entities.db"

        # Create memories table + FTS5
        with closing(sqlite3.connect(str(db_path))) as conn, conn:
            conn.execute("""
                CREATE TABLE memories (
                    id TEXT PRIMARY KEY, content TEXT NOT NULL,
                    compiled_truth TEXT DEFAULT '',
                    timeline TEXT DEFAULT '',
                    summary TEXT, category TEXT DEFAULT 'project',
                    tier TEXT DEFAULT 'active', key_topics TEXT,
                    source TEXT DEFAULT '', embedding BLOB,
                    created_at REAL NOT NULL, accessed_at REAL NOT NULL,
                    expires_at REAL, dream_count INTEGER DEFAULT 0,
                    last_dream_at REAL
                )
            """)
            try:
                conn.execute("""
                    CREATE VIRTUAL TABLE memories_fts USING fts5(
                        content, compiled_truth, summary, key_topics,
                        content=memories, content_rowid=rowid
                    )
                """)
            except Exception:
                pass

            # Insert test memories
            now = time.time()
            memories = [
                (
                    "mem_1",
                    "Ema decided Prism stays domain-agnostic",
                    "Prism stays domain-agnostic",
                    "project",
                    "persist",
                ),
                (
                    "mem_2",
                    "Mango implements vector search for Prism",
                    "Vector search implementation",
                    "project",
                    "active",
                ),
                (
                    "mem_3",
                    "Remembrance V2 uses SQLite and FTS5",
                    "SQLite + FTS5 architecture",
                    "project",
                    "active",
                ),
                (
                    "mem_4",
                    "DilBERT gate classifies memories at 0.929 confidence",
                    "DilBERT gate 0.929",
                    "project",
                    "persist",
                ),
                ("mem_5", "The weather is nice today", "Weather", "general", "cold"),
            ]
            for mem_id, content, summary, category, tier in memories:
                conn.execute(
                    """
                    INSERT INTO memories (id, content, summary, category, tier, created_at, accessed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                    (mem_id, content, summary, category, tier, now, now),
                )

                try:
                    rowid = conn.execute(
                        "SELECT rowid FROM memories WHERE id = ?", (mem_id,)
                    ).fetchone()[0]
                    conn.execute(
                        """
                        INSERT INTO memories_fts (rowid, content, compiled_truth, summary, key_topics)
                        VALUES (?, ?, '', ?, '')
                    """,
                        (rowid, content, summary),
                    )
                except Exception:
                    pass

        # Create entity store
        entity_store = EntityStore(entity_db)

        search = HybridSearch(db_path, entity_store)
        yield {
            "search": search,
            "db_path": db_path,
            "entity_store": entity_store,
        }


class TestKeywordSearch:
    def test_fts5_search(self, search_env):
        results = search_env["search"]._search_keyword("Prism domain-agnostic", None, None, 5)
        assert len(results) > 0
        # Should find the Prism memory
        ids = [r["id"] for r in results]
        assert "mem_1" in ids

    def test_keyword_no_results(self, search_env):
        results = search_env["search"]._search_keyword("xylophone banana", None, None, 5)
        assert len(results) == 0

    def test_keyword_with_category(self, search_env):
        results = search_env["search"]._search_keyword("Prism", "general", None, 5)
        # "Prism" memories are in "project" category, not "general"
        ids = [r["id"] for r in results]
        assert "mem_1" not in ids


class TestTierBoost:
    def test_tier_boost_values(self):
        assert TIER_BOOST["persist"] > TIER_BOOST["active"]
        assert TIER_BOOST["active"] > TIER_BOOST["cold"]

    def test_tier_boost_in_results(self, search_env):
        results = search_env["search"].search("Prism", mode="balanced", limit=10)
        # PERSIST results should generally rank higher than COLD
        if len(results) >= 2:
            # Find persist vs cold results
            persist_scores = [r["score"] for r in results if r.get("tier") == "persist"]
            cold_scores = [r["score"] for r in results if r.get("tier") == "cold"]
            if persist_scores and cold_scores:
                assert max(persist_scores) >= max(cold_scores)


class TestBalancedSearch:
    def test_balanced_returns_results(self, search_env):
        results = search_env["search"].search("Prism domain-agnostic", mode="balanced", limit=5)
        assert len(results) > 0

    def test_balanced_finds_relevant(self, search_env):
        results = search_env["search"].search("DilBERT", mode="balanced", limit=5)
        ids = [r["id"] for r in results]
        assert "mem_4" in ids  # DilBERT memory should be found


class TestContextBuild:
    def test_context_build(self, search_env):
        context = search_env["search"].build_context("Prism architecture", project="prism")
        assert "memories" in context
        assert "entities" in context
        assert "query" in context
        assert context["query"] == "Prism architecture"


class TestCosineSimilarity:
    def test_identical_vectors(self):
        from remembrance_mcp.search.hybrid import HybridSearch

        vec = [1.0, 0.0, 0.0]
        sim = HybridSearch._cosine_similarity(vec, vec)
        assert abs(sim - 1.0) < 0.001

    def test_orthogonal_vectors(self):
        from remembrance_mcp.search.hybrid import HybridSearch

        a = [1.0, 0.0]
        b = [0.0, 1.0]
        sim = HybridSearch._cosine_similarity(a, b)
        assert abs(sim) < 0.001

    def test_opposite_vectors(self):
        from remembrance_mcp.search.hybrid import HybridSearch

        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        sim = HybridSearch._cosine_similarity(a, b)
        assert abs(sim + 1.0) < 0.001

    def test_zero_vector(self):
        from remembrance_mcp.search.hybrid import HybridSearch

        a = [0.0, 0.0]
        b = [1.0, 0.0]
        sim = HybridSearch._cosine_similarity(a, b)
        assert sim == 0.0


class TestVectorConversion:
    def test_bytes_roundtrip(self):
        from remembrance_mcp.search.hybrid import HybridSearch

        vec = [0.1, 0.2, 0.3, 0.4]
        blob = HybridSearch._vector_to_bytes(vec)
        recovered = HybridSearch._bytes_to_vector(blob)
        assert len(recovered) == 4
        for a, b in zip(vec, recovered):
            assert abs(a - b) < 0.001

    def test_empty_vector(self):
        from remembrance_mcp.search.hybrid import HybridSearch

        assert HybridSearch._bytes_to_vector(b"") == []
        assert HybridSearch._vector_to_bytes([]) == b""
