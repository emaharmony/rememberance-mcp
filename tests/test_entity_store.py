"""
Tests for EntityStore — Entity Registry + Typed Edges + Graph Traversal
"""

import sqlite3
import tempfile
import time
from pathlib import Path
import pytest
from recall_mcp.store.edges import EntityStore


@pytest.fixture
def store():
    """Create a temporary EntityStore for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_entities.db"
        yield EntityStore(db_path)


class TestEntityCRUD:
    def test_create_entity(self, store):
        entity_id = store.create_entity("Ema", "person", aliases=["Emmanuel", "rhem"])
        assert entity_id == "ema"

        entity = store.get_entity("ema")
        assert entity is not None
        assert entity["name"] == "Ema"
        assert entity["type"] == "person"
        assert "Emmanuel" in entity["aliases"]
        assert "rhem" in entity["aliases"]

    def test_create_entity_dedup(self, store):
        """Creating same entity twice should not duplicate."""
        id1 = store.create_entity("Prism", "project")
        id2 = store.create_entity("Prism", "project")
        assert id1 == id2

        entities = store.list_entities()
        assert len(entities) == 1

    def test_create_entity_adds_alias_on_dedup(self, store):
        """If name is an alias of existing entity, add as alias."""
        store.create_entity("Ema", "person", aliases=["Emmanuel"])
        # Now 'Emmanuel' is an alias, so creating 'Emmanuel' should find 'ema'
        store.create_entity("Emmanuel", "person")

        entity = store.get_entity("ema")
        assert "Emmanuel" in entity["aliases"]

    def test_slugify(self, store):
        assert store._slugify("Ema") == "ema"
        assert store._slugify("AI Hedge Prism") == "ai-hedge-prism"
        assert store._slugify("DilBERT Gate") == "dilbert-gate"
        assert store._slugify("  spaces  ") == "spaces"

    def test_find_entity_by_slug(self, store):
        store.create_entity("Prism", "project")
        found = store.find_entity("Prism")
        assert found is not None
        assert found["id"] == "prism"

    def test_find_entity_by_alias(self, store):
        store.create_entity("Ema", "person", aliases=["Emmanuel"])
        found = store.find_entity("Emmanuel")
        assert found is not None
        assert found["id"] == "ema"

    def test_find_entity_not_found(self, store):
        found = store.find_entity("nonexistent")
        assert found is None

    def test_update_entity(self, store):
        store.create_entity("Ema", "person")
        store.update_entity(
            "ema", compiled_truth="Senior dev transitioning to AI engineering"
        )

        entity = store.get_entity("ema")
        assert entity["compiled_truth"] == "Senior dev transitioning to AI engineering"

    def test_add_timeline_entry(self, store):
        store.create_entity("Ema", "person")
        store.add_timeline_entry("ema", "Defined Recall V2 architecture", source="Lumi")

        entity = store.get_entity("ema")
        assert "Defined Recall V2 architecture" in entity["timeline"]
        assert "[Source: Lumi]" in entity["timeline"]

    def test_delete_entity_cascades(self, store):
        store.create_entity("Ema", "person")
        store.create_entity("Prism", "project")
        store.add_edge("ema", "prism", "works_on")

        store.delete_entity("ema")
        assert store.get_entity("ema") is None
        edges = store.get_edges("prism")
        assert len(edges) == 0

    def test_search_entities(self, store):
        store.create_entity("Ema", "person")
        store.create_entity("Prism", "project")
        store.create_entity("DilBERT", "concept")

        results = store.search_entities("prism")
        assert len(results) == 1
        assert results[0]["id"] == "prism"

    def test_list_entities_by_type(self, store):
        store.create_entity("Ema", "person")
        store.create_entity("Prism", "project")
        store.create_entity("DilBERT", "concept")

        people = store.list_entities(entity_type="person")
        assert len(people) == 1
        assert people[0]["type"] == "person"


class TestEdges:
    def test_add_edge(self, store):
        store.create_entity("Ema", "person")
        store.create_entity("Prism", "project")

        result = store.add_edge(
            "ema", "prism", "works_on", evidence="Ema leads Prism development"
        )
        assert result is True

        edges = store.get_edges("ema", direction="outgoing")
        assert len(edges) == 1
        assert edges[0]["edge_type"] == "works_on"
        assert edges[0]["target_id"] == "prism"

    def test_add_edge_idempotent(self, store):
        store.create_entity("Ema", "person")
        store.create_entity("Prism", "project")

        store.add_edge("ema", "prism", "works_on")
        store.add_edge("ema", "prism", "works_on")  # duplicate, should be ignored

        edges = store.get_edges("ema", direction="outgoing")
        assert len(edges) == 1

    def test_get_edges_both_directions(self, store):
        store.create_entity("Ema", "person")
        store.create_entity("Prism", "project")

        store.add_edge("ema", "prism", "works_on")

        outgoing = store.get_edges("ema", direction="outgoing")
        assert len(outgoing) == 1

        incoming = store.get_edges("prism", direction="incoming")
        assert len(incoming) == 1

        both = store.get_edges("ema", direction="both")
        assert len(both) == 1

    def test_remove_edge(self, store):
        store.create_entity("Ema", "person")
        store.create_entity("Prism", "project")
        store.add_edge("ema", "prism", "works_on")

        store.remove_edge("ema", "prism", "works_on")
        edges = store.get_edges("ema")
        assert len(edges) == 0

    def test_invalid_edge_type(self, store):
        store.create_entity("Ema", "person")
        store.create_entity("Prism", "project")

        result = store.add_edge("ema", "prism", "invalid_type")
        assert result is False

    def test_edge_type_filter(self, store):
        store.create_entity("Ema", "person")
        store.create_entity("Prism", "project")

        store.add_edge("ema", "prism", "works_on")
        store.add_edge("ema", "prism", "mentions")

        filtered = store.get_edges("ema", edge_type="works_on")
        assert len(filtered) == 1


class TestGraphTraversal:
    def test_neighbors_depth_1(self, store):
        store.create_entity("Ema", "person")
        store.create_entity("Prism", "project")
        store.create_entity("Nemotron", "concept")
        store.add_edge("ema", "prism", "works_on")
        store.add_edge("prism", "nemotron", "depends_on")

        # Depth 1 from Ema: should find Prism but not Nemotron
        result = store.get_neighbors("ema", depth=1)
        entity_ids = [e["id"] for e in result["entities"]]
        assert "ema" in entity_ids
        assert "prism" in entity_ids
        assert "nemotron" not in entity_ids

    def test_neighbors_depth_2(self, store):
        store.create_entity("Ema", "person")
        store.create_entity("Prism", "project")
        store.create_entity("Nemotron", "concept")
        store.add_edge("ema", "prism", "works_on")
        store.add_edge("prism", "nemotron", "depends_on")

        # Depth 2 from Ema: should find both Prism and Nemotron
        result = store.get_neighbors("ema", depth=2)
        entity_ids = [e["id"] for e in result["entities"]]
        assert "ema" in entity_ids
        assert "prism" in entity_ids
        assert "nemotron" in entity_ids


class TestMemoryEntityLinks:
    def test_link_memory_entity(self, store):
        # Need a memories table for FK constraint — use same DB.
        # `with conn:` only commits/rolls back on exit, it does not close
        # the connection — close explicitly so Windows can remove the
        # fixture's TemporaryDirectory at teardown.
        conn = sqlite3.connect(str(store.db_path))
        try:
            with conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS memories (
                        id TEXT PRIMARY KEY, content TEXT NOT NULL,
                        summary TEXT, category TEXT DEFAULT 'project',
                        tier TEXT DEFAULT 'active', key_topics TEXT,
                        source TEXT DEFAULT '', embedding BLOB,
                        created_at REAL NOT NULL, accessed_at REAL NOT NULL,
                        expires_at REAL
                    )
                """)
                conn.execute(
                    "INSERT INTO memories (id, content, created_at, accessed_at) VALUES (?, ?, ?, ?)",
                    (
                        "mem_123",
                        "Ema decided Prism stays domain-agnostic",
                        time.time(),
                        time.time(),
                    ),
                )
        finally:
            conn.close()

        store.create_entity("Ema", "person")
        store.create_entity("Prism", "project")

        result = store.link_memory_entity("mem_123", "ema")
        assert result is True

        entities = store.get_memory_entities("mem_123")
        assert len(entities) == 1
        assert entities[0]["id"] == "ema"

    def test_find_orphans(self, store):
        store.create_entity("Ema", "person")
        store.create_entity("Prism", "project")
        store.add_edge("ema", "prism", "works_on")

        store.create_entity("Orphan", "concept")  # no edges

        orphans = store.find_orphans()
        orphan_ids = [o["id"] for o in orphans]
        assert "orphan" in orphan_ids
        assert "ema" not in orphan_ids
        assert "prism" not in orphan_ids


class TestStats:
    def test_stats_empty(self, store):
        stats = store.stats()
        assert stats["entities"] == 0
        assert stats["edges"] == 0
        assert stats["orphans"] == 0

    def test_stats_with_data(self, store):
        store.create_entity("Ema", "person")
        store.create_entity("Prism", "project")
        store.add_edge("ema", "prism", "works_on")

        stats = store.stats()
        assert stats["entities"] == 2
        assert stats["edges"] == 1
        assert stats["entities_by_type"]["person"] == 1
        assert stats["edges_by_type"]["works_on"] == 1
