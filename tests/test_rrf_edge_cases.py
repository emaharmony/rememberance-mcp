"""
RRF Fusion + Search Edge Case Tests
"""

from recall_mcp.search.hybrid import HybridSearch, TIER_BOOST


class TestRRFFusion:
    """Test reciprocal rank fusion edge cases."""

    def test_rrf_empty_result_lists(self):
        search = HybridSearch.__new__(HybridSearch)
        fused = search._rrf_fuse([], [])
        assert fused == []

    def test_rrf_single_result(self):
        search = HybridSearch.__new__(HybridSearch)
        fts_results = [{"id": "mem_1", "tier": "active", "sources": ["fts5"]}]
        fused = search._rrf_fuse(fts_results)
        assert len(fused) == 1
        assert fused[0]["id"] == "mem_1"

    def test_rrf_duplicate_across_strategies(self):
        """Same memory found by both FTS5 and vector should get combined score."""
        search = HybridSearch.__new__(HybridSearch)
        fts_results = [{"id": "mem_1", "tier": "persist", "sources": ["fts5"]}]
        vec_results = [{"id": "mem_1", "tier": "persist", "sources": ["vector"]}]
        fused = search._rrf_fuse(fts_results, vec_results)
        assert len(fused) == 1
        # Score should be sum of both strategy contributions
        assert fused[0]["score"] > 0

    def test_rrf_tier_boost_persist(self):
        """PERSIST tier should get 1.5x boost in RRF."""
        search = HybridSearch.__new__(HybridSearch)
        persist_results = [{"id": "mem_1", "tier": "persist", "sources": ["fts5"]}]
        cold_results = [{"id": "mem_2", "tier": "cold", "sources": ["fts5"]}]
        # Both rank 1 in their respective lists
        fused = search._rrf_fuse(persist_results, cold_results)
        persist_score = next(r["score"] for r in fused if r["id"] == "mem_1")
        cold_score = next(r["score"] for r in fused if r["id"] == "mem_2")
        assert persist_score > cold_score

    def test_rrf_score_ties(self):
        """When two results have equal RRF score, both should be returned."""
        search = HybridSearch.__new__(HybridSearch)
        results = [
            {"id": "mem_1", "tier": "active", "sources": ["fts5"]},
            {"id": "mem_2", "tier": "active", "sources": ["fts5"]},
        ]
        fused = search._rrf_fuse(results)
        assert len(fused) == 2

    def test_rrf_preserves_sources(self):
        """Sources should be deduplicated in fused results."""
        search = HybridSearch.__new__(HybridSearch)
        r1 = [{"id": "mem_1", "tier": "active", "sources": ["fts5"]}]
        r2 = [{"id": "mem_1", "tier": "active", "sources": ["vector"]}]
        fused = search._rrf_fuse(r1, r2)
        assert set(fused[0]["sources"]) == {"fts5", "vector"}


class TestTierBoostValues:
    def test_tier_boost_ordering(self):
        assert TIER_BOOST["persist"] > TIER_BOOST["active"] > TIER_BOOST["cold"]

    def test_tier_boost_persist_value(self):
        assert TIER_BOOST["persist"] == 1.5

    def test_tier_boost_cold_value(self):
        assert TIER_BOOST["cold"] == 0.5


class TestCosineSimilarityEdgeCases:
    def test_different_length_vectors(self):
        a = [1.0, 0.0]
        b = [1.0, 0.0, 0.0]
        sim = HybridSearch._cosine_similarity(a, b)
        assert sim == 0.0  # Different length = no similarity

    def test_empty_vectors(self):
        sim = HybridSearch._cosine_similarity([], [])
        assert sim == 0.0

    def test_unit_vectors(self):
        a = [0.6, 0.8]  # unit vector
        b = [0.8, 0.6]  # unit vector
        sim = HybridSearch._cosine_similarity(a, b)
        assert abs(sim - 0.96) < 0.01
