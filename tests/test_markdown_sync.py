"""
Tests for MarkdownSync — SQLite ↔ Brain Repo
"""

import tempfile
import time
from pathlib import Path
import pytest
from recall_mcp.store.edges import EntityStore
from recall_mcp.store.markdown import MarkdownSync


@pytest.fixture
def sync_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        brain_dir = Path(tmpdir) / "brain"
        entity_store = EntityStore(db_path)
        sync = MarkdownSync(entity_store, brain_dir=brain_dir)
        yield {
            "sync": sync,
            "entity_store": entity_store,
            "brain_dir": brain_dir,
        }


class TestExportEntity:
    def test_export_person(self, sync_env):
        store = sync_env["entity_store"]
        sync = sync_env["sync"]
        sync_env["brain_dir"]

        store.create_entity(
            "Ema",
            "person",
            aliases=["Emmanuel"],
            compiled_truth="Senior dev transitioning to AI engineering",
        )

        path = sync.export_entity("ema")
        assert path is not None
        assert path.exists()
        assert "people" in str(path)
        assert path.stem == "ema"

    def test_export_project(self, sync_env):
        store = sync_env["entity_store"]
        sync = sync_env["sync"]
        sync_env["brain_dir"]

        store.create_entity(
            "Prism", "project", compiled_truth="Event-driven AI framework"
        )

        path = sync.export_entity("prism")
        assert path is not None
        assert "projects" in str(path)

    def test_export_page_format(self, sync_env):
        store = sync_env["entity_store"]
        sync = sync_env["sync"]

        store.create_entity(
            "Ema",
            "person",
            aliases=["Emmanuel"],
            compiled_truth="Senior dev transitioning to AI engineering",
            tier="persist",
        )

        path = sync.export_entity("ema")
        content = path.read_text()

        assert "type: person" in content
        assert "Emmanuel" in content
        assert "tier: persist" in content
        assert "Senior dev transitioning to AI engineering" in content
        assert "## Executive Summary" in content
        assert "## Timeline" in content

    def test_export_nonexistent(self, sync_env):
        sync = sync_env["sync"]
        path = sync.export_entity("nonexistent")
        assert path is None


class TestExportAll:
    def test_export_all(self, sync_env):
        store = sync_env["entity_store"]
        sync = sync_env["sync"]

        store.create_entity("Ema", "person")
        store.create_entity("Prism", "project")
        store.create_entity("DilBERT", "concept")

        result = sync.export_all()
        assert result["exported"] == 3
        assert result["errors"] == 0


class TestImportEdits:
    def test_import_edits_no_changes(self, sync_env):
        store = sync_env["entity_store"]
        sync = sync_env["sync"]

        store.create_entity("Ema", "person", compiled_truth="Original truth")
        sync.export_entity("ema")

        # No manual edits → no imports
        result = sync.import_edits()
        assert result["imported"] == 0

    def test_import_edits_with_changes(self, sync_env):
        store = sync_env["entity_store"]
        sync = sync_env["sync"]
        brain_dir = sync_env["brain_dir"]

        store.create_entity("Ema", "person", compiled_truth="Original truth")
        sync.export_entity("ema")

        # Manually edit the file
        page_path = brain_dir / "entities" / "people" / "ema.md"
        content = page_path.read_text()
        content = content.replace("Original truth", "Updated via manual edit")
        page_path.write_text(content)

        # Touch the file to update mtime
        import os

        os.utime(str(page_path), (time.time() + 100, time.time() + 100))

        result = sync.import_edits()
        assert result["imported"] == 1

        # Check that SQLite was updated
        entity = store.get_entity("ema")
        assert "Updated via manual edit" in entity["compiled_truth"]


class TestBuildPage:
    def test_page_structure(self, sync_env):
        sync = sync_env["sync"]
        entity = {
            "id": "test",
            "name": "Test",
            "type": "concept",
            "aliases": ["alias1"],
            "compiled_truth": "This is the compiled truth",
            "timeline": "- **2026-05-19** | Test entry\n",
            "tier": "active",
        }

        page = sync._build_page(entity)
        assert "type: concept" in page
        assert "This is the compiled truth" in page
        assert "Test entry" in page


class TestParseCompiledTruth:
    def test_parse_truth(self, sync_env):
        sync = sync_env["sync"]

        md_content = """---
type: person
---

## Executive Summary
This is the compiled truth for testing.

---

## Timeline
- entry
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(md_content)
            f.flush()

            truth = sync._parse_compiled_truth(Path(f.name))
            assert truth == "This is the compiled truth for testing."
