"""
Tests for FactStore — Structured Claims with Provenance
"""

import tempfile
import time
from contextlib import closing
from pathlib import Path
import pytest
from recall_mcp.store.facts import FactStore


@pytest.fixture
def fact_store():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_facts.db"
        # Need entities table for FK constraint
        import sqlite3

        with closing(sqlite3.connect(str(db_path))) as conn, conn:
            conn.execute("""
                CREATE TABLE entities (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    type TEXT NOT NULL, aliases TEXT DEFAULT '[]',
                    compiled_truth TEXT DEFAULT '', timeline TEXT DEFAULT '',
                    tier TEXT NOT NULL DEFAULT 'active',
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                )
            """)
            conn.execute(
                "INSERT INTO entities (id, name, type, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                ("ema", "Ema", "person", time.time(), time.time()),
            )
        yield FactStore(db_path)


class TestFactAssertions:
    def test_assert_fact(self, fact_store):
        fid = fact_store.assert_fact("ema", "role", "lead developer", "session 1")
        assert fid is not None

        fact = fact_store.get_current_fact("ema", "role")
        assert fact is not None
        assert fact["claim_value"] == "lead developer"

    def test_supersede_fact(self, fact_store):
        fact_store.assert_fact("ema", "role", "developer", "session 1")
        time.sleep(0.01)  # ensure different observed_at
        fact_store.assert_fact("ema", "role", "AI engineer", "session 2")

        current = fact_store.get_current_fact("ema", "role")
        assert current["claim_value"] == "AI engineer"

        # Old fact should be superseded
        history = fact_store.get_fact_history("ema", "role")
        assert len(history) == 2
        # Order is DESC — newest first
        assert history[0]["superseded_at"] is None  # current (newest)
        assert history[1]["superseded_at"] is not None  # oldest superseded

    def test_same_value_no_supersede(self, fact_store):
        fact_store.assert_fact("ema", "role", "lead developer", "session 1")
        fact_store.assert_fact("ema", "role", "lead developer", "session 2")

        # Should still have only current fact
        current = fact_store.get_current_fact("ema", "role")
        assert current["claim_value"] == "lead developer"

    def test_multiple_keys(self, fact_store):
        fact_store.assert_fact("ema", "role", "lead developer", "session 1")
        fact_store.assert_fact("ema", "stack", "Go + Python", "session 1")

        facts = fact_store.get_entity_facts("ema")
        assert len(facts) == 2


class TestContradictions:
    def test_no_contradictions(self, fact_store):
        fact_store.assert_fact("ema", "role", "lead developer", "session 1")
        contradictions = fact_store.find_contradictions()
        assert len(contradictions) == 0

    def test_with_contradictions(self, fact_store):
        # Create two conflicting current facts by inserting directly
        now = time.time()
        import sqlite3

        with closing(sqlite3.connect(str(fact_store.db_path))) as conn, conn:
            conn.execute(
                "INSERT INTO facts (id, entity_id, claim_key, claim_value, source, confidence, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("f1", "ema", "role", "developer", "s1", 0.9, now),
            )
            conn.execute(
                "INSERT INTO facts (id, entity_id, claim_key, claim_value, source, confidence, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("f2", "ema", "role", "AI engineer", "s2", 0.8, now + 1),
            )

        contradictions = fact_store.find_contradictions()
        assert len(contradictions) == 1
        assert contradictions[0]["entity_id"] == "ema"
        resolution = fact_store.resolve_contradictions()
        assert resolution["facts_superseded"] == 1
        current = fact_store.get_current_fact("ema", "role")
        assert current["id"] == "f1"
        history = fact_store.get_fact_history("ema", "role")
        assert sum(fact["superseded_at"] is not None for fact in history) == 1


class TestStats:
    def test_stats_empty(self, fact_store):
        stats = fact_store.stats()
        assert stats["total_facts"] == 0
        assert stats["current_facts"] == 0

    def test_stats_with_facts(self, fact_store):
        fact_store.assert_fact("ema", "role", "lead developer", "session 1")
        stats = fact_store.stats()
        assert stats["total_facts"] == 1
        assert stats["current_facts"] == 1
