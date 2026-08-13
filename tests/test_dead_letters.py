"""Tests for the recall-admin dead-letters command."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from recall_mcp.admin import build_parser, command_dead_letters
from recall_mcp.config import Settings
from recall_mcp.store.store import MemoryStore, OutboxJob


def _make_store(tmp_path: Path) -> MemoryStore:
    store = MemoryStore(tmp_path / "memory.db")
    # Ensure outbox_jobs table exists (created by migrations in store init).
    return store


def _insert_dead_job(db_path: Path, job_id: str = "dead_1") -> str:
    """Insert a fake dead-lettered job for testing."""
    capture_id = f"capture_{job_id}"
    with sqlite3.connect(str(db_path)) as conn:
        # Insert parent raw_capture first (FK constraint).
        conn.execute(
            """
            INSERT OR IGNORE INTO raw_captures (id, content, source, status, received_at, updated_at)
            VALUES (?, 'test content', 'test', 'processing', 1700000000.0, 1700000010.0)
            """,
            (capture_id,),
        )
        conn.execute(
            """
            INSERT INTO outbox_jobs (id, raw_capture_id, status, attempts, last_error, created_at, updated_at, available_at)
            VALUES (?, ?, 'dead', 5, 'EmbeddingError: test failure', 1700000000.0, 1700000010.0, 1700000000.0)
            """,
            (job_id, capture_id),
        )
        conn.commit()
    return job_id


class TestDeadLettersCommand:
    def test_list_dead_letters_empty(self, tmp_path):
        store = _make_store(tmp_path)
        settings = Settings(DB_PATH=tmp_path / "memory.db", EMBEDDINGS_ENABLED=False)
        parser = build_parser()
        args = parser.parse_args(["dead-letters"])
        code = command_dead_letters(settings, args)
        assert code == 0

    def test_list_dead_letters_with_entries(self, tmp_path):
        store = _make_store(tmp_path)
        _insert_dead_job(tmp_path / "memory.db")
        settings = Settings(DB_PATH=tmp_path / "memory.db", EMBEDDINGS_ENABLED=False)
        parser = build_parser()
        args = parser.parse_args(["dead-letters"])
        code = command_dead_letters(settings, args)
        assert code == 0

    def test_replay_dead_letters(self, tmp_path):
        store = _make_store(tmp_path)
        job_id = _insert_dead_job(tmp_path / "memory.db")
        settings = Settings(DB_PATH=tmp_path / "memory.db", EMBEDDINGS_ENABLED=False)
        parser = build_parser()
        args = parser.parse_args(["dead-letters", "--replay"])
        code = command_dead_letters(settings, args)
        assert code == 0
        # Verify the job was retried (status changed from 'dead')
        with sqlite3.connect(str(tmp_path / "memory.db")) as conn:
            row = conn.execute(
                "SELECT status FROM outbox_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            # retry_dead_outbox_job sets it to 'pending' or similar
            assert row[0] != "dead"

    def test_replay_specific_job_id(self, tmp_path):
        store = _make_store(tmp_path)
        _insert_dead_job(tmp_path / "memory.db", "dead_1")
        _insert_dead_job(tmp_path / "memory.db", "dead_2")
        settings = Settings(DB_PATH=tmp_path / "memory.db", EMBEDDINGS_ENABLED=False)
        parser = build_parser()
        args = parser.parse_args(["dead-letters", "--replay", "--job-id", "dead_1"])
        code = command_dead_letters(settings, args)
        assert code == 0