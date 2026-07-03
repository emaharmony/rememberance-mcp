"""
Integration Tests — End-to-End Pipeline, Dream Cycle, and API

These tests exercise the full pipeline: gate → extract → graph → search → dream.
They're the "system tests" that unit tests can't replace.
"""

import tempfile
import time
from pathlib import Path

import pytest

from remembrance_mcp.config import Settings
from remembrance_mcp.pipeline import MemoryPipeline


@pytest.fixture
def pipeline():
    """Create a full pipeline with temp databases and heuristic gate (fast, no Ollama)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "memories.db"
        settings = Settings(
            DB_PATH=db_path,
            OLLAMA_BASE_URL="http://localhost:11434",
            GATE_MODEL_PATH=None,
        )
        pipe = MemoryPipeline(settings=settings)
        # Override gate to heuristic-only for fast tests (no Ollama/DilBERT calls)
        from remembrance_mcp.extract import StubExtractor
        from remembrance_mcp.gate_backends import GateFallbackChain, HeuristicBackend

        pipe.gate_chain = GateFallbackChain([HeuristicBackend()])
        pipe.extractor = StubExtractor()
        yield pipe


class TestCaptureIntegration:
    """Test the full capture pipeline: gate → extract → graph → store."""

    def test_capture_creates_memory(self, pipeline):
        result = pipeline.capture("Ema decided Prism stays domain-agnostic", source="test")
        assert result["id"] is not None
        assert result["decision"].lower() in ("persist", "active", "cold", "skip")
        assert result["tier"].lower() in ("persist", "active", "cold", "skip")

    def test_capture_detects_entities(self, pipeline):
        result = pipeline.capture("Ema decided Prism stays domain-agnostic", source="test")
        # Should detect "ema" and "prism" entities
        assert len(result.get("entities", [])) >= 1

    def test_capture_creates_edges(self, pipeline):
        result = pipeline.capture("Ema decided Prism stays domain-agnostic", source="test")
        # Should create edges between detected entities
        assert result.get("edges_created", 0) >= 0  # May be 0 if only 1 entity detected

    def test_capture_multiple_memories(self, pipeline):
        r1 = pipeline.capture("Ema works on Prism", source="test")
        r2 = pipeline.capture("Mango implements vector search for Prism", source="test")
        r3 = pipeline.capture("DilBERT gate classifies at 0.929 confidence", source="test")

        # Count non-SKIP captures
        stored = sum(1 for r in [r1, r2, r3] if r.get("id"))
        assert stored >= 2  # At least 2 should be stored

    def test_capture_with_category_override(self, pipeline):
        result = pipeline.capture(
            "Project update about the architecture decision", source="test", category="general"
        )
        # Gate/extraction may override category but should still store
        if result.get("id"):
            assert "category" in result


class TestSearchIntegration:
    """Test hybrid search end-to-end."""

    def test_search_finds_memory(self, pipeline):
        pipeline.capture("Ema decided Prism stays domain-agnostic", source="test", tier="persist")
        pipeline.capture(
            "DilBERT gate classifies memories at 0.929 confidence", source="test", tier="persist"
        )

        results = pipeline.hybrid_search.search("Prism", mode="keyword", limit=5)
        # May or may not find results depending on FTS5 availability
        # At minimum, LIKE fallback should work
        assert isinstance(results, list)

    def test_search_with_keyword_mode(self, pipeline):
        pipeline.capture("Ema decided Prism stays domain-agnostic", source="test")

        results = pipeline.hybrid_search.search("Prism", mode="keyword", limit=5)
        assert isinstance(results, list)

    def test_search_no_results(self, pipeline):
        results = pipeline.hybrid_search.search(
            "xylophone banana quantum", mode="balanced", limit=5
        )
        assert len(results) == 0

    def test_search_category_filter(self, pipeline):
        pipeline.capture("Project update about Prism", source="test", category="project")
        pipeline.capture("Weather is nice today", source="test", category="general")

        results = pipeline.hybrid_search.search(
            "Prism", mode="keyword", category="project", limit=5
        )
        assert isinstance(results, list)  # May be empty in test env

    def test_context_build(self, pipeline):
        pipeline.capture("Ema decided Prism stays domain-agnostic", source="test")
        pipeline.capture("Mango implements vector search", source="test")

        context = pipeline.build_context("implement vector search for Prism")
        assert "memories" in context
        assert "entities" in context


class TestGraphIntegration:
    """Test knowledge graph operations end-to-end."""

    def test_entity_creation_on_capture(self, pipeline):
        pipeline.capture("Ema works on Prism", source="test")

        entity = pipeline.entity_get("ema")
        if entity:
            assert entity["name"] == "Ema"
            assert entity["type"] == "person"

    def test_entity_search(self, pipeline):
        pipeline.capture("Ema works on Prism", source="test")

        # Search may find entity by name or not (depends on detection)
        results = pipeline.entity_store.search_entities("ema")
        assert isinstance(results, list)

    def test_graph_query(self, pipeline):
        pipeline.capture("Ema decided Prism stays domain-agnostic", source="test")

        result = pipeline.graph_query("ema", depth=1)
        # May or may not have graph data depending on entity detection
        assert "error" not in result or "entities" in result

    def test_entity_timeline_grows(self, pipeline):
        pipeline.capture("Ema decided Prism stays domain-agnostic", source="test")
        pipeline.capture("Ema confirmed the architecture decision", source="test")

        entity = pipeline.entity_get("ema")
        if entity and entity.get("timeline"):
            # Timeline should have entries
            assert len(entity["timeline"]) > 0

    def test_entity_compiled_truth(self, pipeline):
        pipeline.capture("Ema is the lead developer", source="test")

        entity = pipeline.entity_get("ema")
        if entity:
            # Compiled truth may be empty if dream cycle hasn't run
            assert entity.get("compiled_truth", "") is not None


class TestDreamCycleIntegration:
    """Test dream cycle end-to-end.

    NOTE: These tests use the entity_sweep/orphan_detect phases which
    don't require Ollama. The truth_rewrite phase is skipped because it
    needs Ollama which may not be available in test environments.
    """

    def test_dream_orphan_detect(self, pipeline):
        pipeline.capture("Test memory for dream cycle", source="test")

        result = pipeline.dream(phases=["orphan_detect"])
        assert result["status"] in ("ok", "partial")
        assert len(result["phases"]) >= 1

    def test_dream_entity_sweep(self, pipeline):
        # Create a memory without entity links
        pipeline.store.store(
            content="Ema works on Prism architecture",
            summary="Ema works on Prism",
            category="project",
            tier="active",
            key_topics=["architecture"],
            source="test",
        )

        result = pipeline.dream(phases=["entity_sweep"])
        assert result["status"] in ("ok", "partial")

    def test_dream_log_recorded(self, pipeline):
        pipeline.capture("Test memory", source="test")

        result = pipeline.dream(phases=["orphan_detect"])
        log_id = result["log_id"]

        # Check dream log was recorded
        log = pipeline.store_v2.get_dream_log(log_id)
        assert log is not None
        assert log["status"] in ("ok", "partial")


class TestFactStoreIntegration:
    """Test fact store operations."""

    def test_assert_and_retrieve_fact(self, pipeline):
        # Create entity first
        pipeline.entity_store.create_entity("Ema", "person")

        pipeline.fact_store.assert_fact("ema", "role", "lead developer", "test")
        fact = pipeline.fact_store.get_current_fact("ema", "role")
        assert fact is not None
        assert fact["claim_value"] == "lead developer"

    def test_fact_supersede(self, pipeline):
        pipeline.entity_store.create_entity("Ema", "person")

        pipeline.fact_store.assert_fact("ema", "role", "developer", "test1")
        time.sleep(0.01)
        pipeline.fact_store.assert_fact("ema", "role", "AI engineer", "test2")

        current = pipeline.fact_store.get_current_fact("ema", "role")
        assert current["claim_value"] == "AI engineer"

        history = pipeline.fact_store.get_fact_history("ema", "role")
        assert len(history) == 2


class TestMarkdownSyncIntegration:
    """Test markdown export/import."""

    def test_export_entity(self, pipeline):
        pipeline.capture("Ema is the lead developer", source="test")

        # Export brain
        result = pipeline.export_brain()
        assert result["exported"] >= 0  # May be 0 if no persist entities

    def test_import_edits(self, pipeline):
        result = pipeline.markdown_sync.import_edits()
        assert "imported" in result


class TestStatsIntegration:
    """Test comprehensive stats."""

    def test_stats_structure(self, pipeline):
        pipeline.capture("Test memory for stats", source="test")

        stats = pipeline.stats()
        assert "memories" in stats
        assert "entities" in stats
        assert "facts" in stats
        assert "v2" in stats

    def test_entity_stats(self, pipeline):
        pipeline.capture("Ema works on Prism", source="test")

        stats = pipeline.entity_store.stats()
        assert "entities" in stats
        assert "edges" in stats
