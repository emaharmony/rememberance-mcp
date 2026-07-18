"""
Tests for EntityDetector — Zero-LLM Entity Extraction
"""

import pytest
from recall_mcp.graph.entity import EntityDetector


@pytest.fixture
def detector():
    return EntityDetector()


class TestDetectKnownEntities:
    def test_detect_person(self, detector):
        result = detector.detect("Ema decided to use Go for the new project")
        names = [e.name for e in result]
        assert "ema" in names

    def test_detect_project(self, detector):
        result = detector.detect("Prism stays domain-agnostic")
        names = [e.name for e in result]
        assert "prism" in names

    def test_detect_multiple(self, detector):
        result = detector.detect("Ema decided Prism stays domain-agnostic")
        names = [e.name for e in result]
        assert "ema" in names
        assert "prism" in names

    def test_detect_alias(self, detector):
        result = detector.detect("Emmanuel is working on the project")
        names = [e.name for e in result]
        assert "ema" in names  # alias resolves to canonical

    def test_detect_no_entities(self, detector):
        result = detector.detect("the weather is nice today")
        # Should have very few or no high-confidence detections
        high_conf = [e for e in result if e.confidence >= 0.8]
        assert len(high_conf) == 0


class TestEdgeTypeInference:
    def test_decided_about(self, detector):
        result = detector.detect("Ema decided Prism stays domain-agnostic")
        ema = [e for e in result if e.name == "ema"][0]
        assert ema.edge_type == "decided_about"

    def test_works_on(self, detector):
        result = detector.detect("Mango works on Prism vector search")
        mango = [e for e in result if e.name == "mango"][0]
        assert mango.edge_type == "works_on"

    def test_depends_on(self, detector):
        result = detector.detect("Prism uses NATS for event bus")
        prism = [e for e in result if e.name == "prism"][0]
        assert prism.edge_type == "depends_on"

    def test_mentions_default(self, detector):
        result = detector.detect("Ema and Prism are related")
        ema = [e for e in result if e.name == "ema"][0]
        assert ema.edge_type == "mentions"


class TestEntityTypes:
    def test_person_type(self, detector):
        result = detector.detect("Ema is the lead developer")
        ema = [e for e in result if e.name == "ema"][0]
        assert ema.entity_type == "person"

    def test_project_type(self, detector):
        result = detector.detect("Prism is an event-driven framework")
        prism = [e for e in result if e.name == "prism"][0]
        assert prism.entity_type == "project"

    def test_concept_type(self, detector):
        result = detector.detect("DilBERT gate classifies memories")
        dilbert = [e for e in result if e.name == "dilbert"][0]
        assert dilbert.entity_type == "concept"

    def test_tool_type(self, detector):
        result = detector.detect("Ollama runs the models")
        ollama = [e for e in result if e.name == "ollama"][0]
        assert ollama.entity_type == "tool"


class TestContextExtraction:
    def test_context_snippet(self, detector):
        result = detector.detect(
            "Ema decided Prism stays domain-agnostic for all adapters"
        )
        ema = [e for e in result if e.name == "ema"][0]
        assert "decided" in ema.context or "Prism" in ema.context


class TestRegistryLookup:
    def test_uses_entity_store(self):
        """When entity_store is provided, detect registered entities."""
        from recall_mcp.store.edges import EntityStore
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            store = EntityStore(Path(d) / "test.db")
            store.create_entity("CustomProject", "project")

            detector = EntityDetector(entity_store=store)
            result = detector.detect("We deployed CustomProject yesterday")
            names = [e.name for e in result]
            assert "CustomProject" in names
