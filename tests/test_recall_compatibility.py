"""Rename safety and legacy-compatibility tests."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import warnings
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from recall_mcp.compat import (
    RecallCompatibilityWarning,
    _reset_warning_state,
    get_env,
    resolve_home,
)
from recall_mcp.config import Settings
from recall_mcp.pipeline import MemoryPipeline


@pytest.fixture(autouse=True)
def clean_rename_environment(monkeypatch):
    for name in (
        "RECALL_HOME",
        "REMEMBRANCE_HOME",
        "RECALL_GATE_BACKENDS",
        "REMEMBRANCE_GATE_BACKENDS",
        "RECALL_TEST_VALUE",
        "REMEMBRANCE_TEST_VALUE",
    ):
        monkeypatch.delenv(name, raising=False)
    _reset_warning_state()
    Settings._instance = None
    yield
    Settings._instance = None
    _reset_warning_state()


@pytest.mark.parametrize(
    ("canonical", "legacy", "expected"),
    [
        ("canonical", None, "canonical"),
        (None, "legacy", "legacy"),
        ("canonical", "legacy", "canonical"),
        (None, None, "default"),
    ],
)
def test_environment_precedence(monkeypatch, canonical, legacy, expected):
    if canonical is not None:
        monkeypatch.setenv("RECALL_TEST_VALUE", canonical)
    if legacy is not None:
        monkeypatch.setenv("REMEMBRANCE_TEST_VALUE", legacy)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert get_env("TEST_VALUE", "default") == expected

    legacy_warnings = [
        item for item in caught if issubclass(item.category, RecallCompatibilityWarning)
    ]
    assert bool(legacy_warnings) is (canonical is None and legacy is not None)


@contextmanager
def _temporary_home():
    workspace_tmp = Path.cwd() / ".tmp"
    workspace_tmp.mkdir(exist_ok=True)
    raw_home = tempfile.mkdtemp(dir=workspace_tmp)
    try:
        yield raw_home
    finally:
        shutil.rmtree(raw_home, ignore_errors=True)


def test_home_prefers_explicit_recall(monkeypatch):
    with _temporary_home() as raw_home:
        home = Path(raw_home)
        explicit = home / "explicit-recall"
        (home / ".recall").mkdir()
        (home / ".remembrance").mkdir()
        monkeypatch.setenv("RECALL_HOME", str(explicit))
        monkeypatch.setenv("REMEMBRANCE_HOME", str(home / "explicit-legacy"))
        assert resolve_home(home) == explicit


def test_home_prefers_existing_recall_before_explicit_legacy(monkeypatch):
    with _temporary_home() as raw_home:
        home = Path(raw_home)
        canonical = home / ".recall"
        canonical.mkdir()
        monkeypatch.setenv("REMEMBRANCE_HOME", str(home / "explicit-legacy"))
        assert resolve_home(home) == canonical


def test_home_uses_explicit_legacy_when_no_recall_exists(monkeypatch):
    with _temporary_home() as raw_home:
        home = Path(raw_home)
        legacy = home / "explicit-legacy"
        monkeypatch.setenv("REMEMBRANCE_HOME", str(legacy))
        with pytest.warns(RecallCompatibilityWarning, match="existing Remembrance"):
            assert resolve_home(home) == legacy


def test_home_uses_existing_legacy_in_place(monkeypatch):
    with _temporary_home() as raw_home:
        home = Path(raw_home)
        legacy = home / ".remembrance"
        legacy.mkdir()
        with pytest.warns(RecallCompatibilityWarning, match="existing Remembrance"):
            assert resolve_home(home) == legacy
        assert not (home / ".recall").exists()


def test_home_defaults_to_new_recall_location(monkeypatch):
    with _temporary_home() as raw_home:
        home = Path(raw_home)
        assert resolve_home(home) == home / ".recall"


def test_legacy_namespace_delegates_to_canonical_modules():
    code = """
import remembrance_mcp
from recall_mcp.pipeline import MemoryPipeline as CanonicalPipeline
from remembrance_mcp.pipeline import MemoryPipeline as LegacyPipeline
from recall_mcp.search.hybrid import HybridSearch as CanonicalSearch
from remembrance_mcp.search.hybrid import HybridSearch as LegacySearch
assert LegacyPipeline is CanonicalPipeline
assert LegacySearch is CanonicalSearch
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path.cwd() / "src")
    result = subprocess.run(
        [sys.executable, "-W", "always", "-c", code],
        cwd=Path.cwd(),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "remembrance_mcp is deprecated" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("module", ["recall_mcp", "remembrance_mcp"])
def test_module_help(module):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path.cwd() / "src")
    result = subprocess.run(
        [sys.executable, "-m", module, "--help"],
        cwd=Path.cwd(),
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "Recall MCP stdio server" in result.stdout
    if module == "remembrance_mcp":
        assert "deprecated" in result.stderr


def test_legacy_database_remains_visible_without_a_competing_database(monkeypatch):
    with _temporary_home() as raw_home:
        home = Path(raw_home)
        legacy_home = home / ".remembrance"
        legacy_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.setenv("RECALL_GATE_BACKENDS", "heuristic")

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RecallCompatibilityWarning)
            from remembrance_mcp.config import Settings as LegacySettings
            from remembrance_mcp.pipeline import MemoryPipeline as LegacyPipeline

        with pytest.warns(RecallCompatibilityWarning, match="existing Remembrance"):
            legacy = LegacyPipeline(settings=LegacySettings())
        memory_id = legacy.store.store(
            content="Legacy database memory",
            summary="Legacy memory",
            category="project",
            tier="persist",
            key_topics=["legacy"],
            source="compat-test",
        )
        source_entity = legacy.entity_store.create_entity("Legacy Source", "project")
        target_entity = legacy.entity_store.create_entity("Legacy Target", "concept")
        legacy.entity_store.add_edge(
            source_entity,
            target_entity,
            "related_to",
            evidence="compat-test",
        )
        legacy.fact_store.assert_fact(
            source_entity,
            "status",
            "preserved",
            "compat-test",
        )
        log_id = legacy.store_v2.start_dream_log()
        legacy.store_v2.complete_dream_log(
            log_id,
            "ok",
            ["orphan_detect"],
            {"processed": 1},
        )

        with closing(sqlite3.connect(legacy.settings.DB_PATH)) as conn, conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS chunks "
                "(id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, content TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO chunks (id, memory_id, content) VALUES (?, ?, ?)",
                ("chunk_legacy", memory_id, "Legacy chunk"),
            )

        Settings._instance = None
        _reset_warning_state()
        with pytest.warns(RecallCompatibilityWarning, match="existing Remembrance"):
            recall_settings = Settings()
        recall = MemoryPipeline(settings=recall_settings)

        assert recall.settings.DB_PATH == legacy.settings.DB_PATH
        assert recall.store.get(memory_id)["content"] == "Legacy database memory"
        assert recall.entity_store.get_entity(source_entity)["name"] == "Legacy Source"
        assert any(
            edge["target_id"] == target_entity
            for edge in recall.entity_store.get_edges(source_entity)
        )
        assert (
            recall.fact_store.get_current_fact(source_entity, "status")["claim_value"]
            == "preserved"
        )
        assert recall.store_v2.get_dream_log(log_id)["status"] == "ok"
        with closing(sqlite3.connect(recall.settings.DB_PATH)) as conn, conn:
            chunk = conn.execute(
                "SELECT content FROM chunks WHERE id = ?", ("chunk_legacy",)
            ).fetchone()
        assert chunk == ("Legacy chunk",)
        assert not (home / ".recall").exists()
