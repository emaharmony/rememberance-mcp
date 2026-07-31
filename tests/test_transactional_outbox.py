"""Behavior coverage for the Phase 0 transactional capture outbox."""

from __future__ import annotations

import json
import sqlite3
import time
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from recall_mcp.admin import command_outbox
from recall_mcp.config import Settings
from recall_mcp.embeddings import EmbeddingError, EmbeddingResult
from recall_mcp.extract import StubExtractor
from recall_mcp.gate_backends import GateFallbackChain, HeuristicBackend
from recall_mcp.pipeline import MemoryPipeline
from recall_mcp.store import MemoryStore
from recall_mcp.store.migrations import MIGRATIONS, run_migrations


def _pipeline(tmp_path, **overrides) -> MemoryPipeline:
    values = {
        "BASE_DIR": tmp_path,
        "DB_PATH": tmp_path / "memory.db",
        "EMBEDDINGS_ENABLED": False,
        "CAPTURE_PROCESSING_TIMEOUT": 2.0,
        "OUTBOX_POLL_INTERVAL": 0.01,
        "OUTBOX_RETRY_BASE_SECONDS": 0.01,
    }
    values.update(overrides)
    settings = Settings(**values)
    pipeline = MemoryPipeline(settings)
    pipeline.gate_chain = GateFallbackChain([HeuristicBackend()])
    pipeline.extractor = StubExtractor()
    return pipeline


def test_enqueue_is_atomic_when_outbox_insert_fails(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            CREATE TRIGGER force_outbox_failure
            BEFORE INSERT ON outbox_jobs BEGIN
                SELECT RAISE(ABORT, 'forced outbox failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced outbox failure"):
        store.enqueue_capture("must roll back", source="test")

    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM raw_captures").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM outbox_jobs").fetchone()[0] == 0


def test_migration_backfills_only_pending_raw_captures(tmp_path):
    db_path = tmp_path / "memory.db"
    run_migrations(db_path, migrations=MIGRATIONS[:4])
    now = time.time()
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO raw_captures (
                id, content, status, received_at, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                ("pending", "recover me", "pending", now, now),
                ("failed", "leave me", "failed", now, now),
            ],
        )

    result = run_migrations(db_path)

    assert [migration.version for migration in result.applied] == [5]
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT raw_capture_id, status FROM outbox_jobs").fetchall()
    assert rows == [("pending", "pending")]


def test_concurrent_claim_is_exclusive_and_expired_lease_is_reclaimed(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    _capture_id, job_id = store.enqueue_capture("lease me")
    claimed_at = time.time() + 1
    barrier = Barrier(4)

    def claim():
        barrier.wait()
        return store.claim_outbox_job(lease_seconds=10, now=claimed_at)

    with ThreadPoolExecutor(max_workers=4) as executor:
        claims = list(executor.map(lambda _index: claim(), range(4)))

    claimed = [job for job in claims if job is not None]
    assert len(claimed) == 1
    assert claimed[0].id == job_id
    assert claimed[0].attempts == 1
    assert store.claim_outbox_job(lease_seconds=10, now=claimed_at + 9) is None
    reclaimed = store.claim_outbox_job(lease_seconds=10, now=claimed_at + 11)
    assert reclaimed is not None
    assert reclaimed.id == job_id
    assert reclaimed.attempts == 2


def test_completion_rolls_back_raw_and_job_together(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    capture_id, job_id = store.enqueue_capture("complete atomically")
    job = store.claim_outbox_job(lease_seconds=10)
    assert job is not None
    store.ensure_capture_memory(
        job,
        gate_decision="ACTIVE",
        gate_confidence=0.9,
        gate_backend="test",
        gate_fallback_used=False,
        category="project",
        tier="active",
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            CREATE TRIGGER force_completion_failure
            BEFORE UPDATE OF status ON raw_captures
            WHEN NEW.status = 'complete' BEGIN
                SELECT RAISE(ABORT, 'forced completion failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced completion failure"):
        store.complete_outbox_job(job, capture_id)

    with sqlite3.connect(store.db_path) as conn:
        raw = conn.execute(
            "SELECT status FROM raw_captures WHERE id = ?", (capture_id,)
        ).fetchone()
        outbox = conn.execute(
            "SELECT status FROM outbox_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    assert raw == ("processing",)
    assert outbox == ("processing",)


def test_retry_dead_and_manual_requeue_state_machine(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    capture_id, job_id = store.enqueue_capture("retry me")
    claimed_at = time.time() + 1
    first = store.claim_outbox_job(lease_seconds=10, now=claimed_at)
    assert first is not None
    store.record_gate_result(
        first,
        gate_decision="ACTIVE",
        gate_confidence=0.91,
        gate_backend="test-gate",
        gate_fallback_used=True,
    )

    status, delay = store.fail_outbox_job(
        first,
        "temporary",
        max_attempts=2,
        retry_base_seconds=2,
        now=claimed_at + 1,
    )
    assert (status, delay) == ("retry", 2)
    assert store.claim_outbox_job(lease_seconds=10, now=claimed_at + 2) is None

    second = store.claim_outbox_job(lease_seconds=10, now=claimed_at + 3)
    assert second is not None
    assert second.gate_decision == "ACTIVE"
    assert second.gate_confidence == 0.91
    assert second.gate_backend == "test-gate"
    assert second.gate_fallback_used is True
    status, delay = store.fail_outbox_job(
        second,
        "exhausted",
        max_attempts=2,
        retry_base_seconds=2,
        now=claimed_at + 4,
    )
    assert (status, delay) == ("dead", 0)
    assert store.outbox_job(job_id)["status"] == "dead"
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute(
            "SELECT status FROM raw_captures WHERE id = ?", (capture_id,)
        ).fetchone() == ("failed",)

    assert store.retry_dead_outbox_job(job_id) is True
    assert store.retry_dead_outbox_job(job_id) is False
    job = store.outbox_job(job_id)
    assert job["status"] == "pending"
    assert job["attempts"] == 0


def test_pending_job_is_recovered_by_a_new_pipeline(tmp_path):
    settings = Settings(
        BASE_DIR=tmp_path,
        DB_PATH=tmp_path / "memory.db",
        EMBEDDINGS_ENABLED=False,
        CAPTURE_PROCESSING_TIMEOUT=0.01,
        OUTBOX_POLL_INTERVAL=0.01,
    )
    first = MemoryPipeline(settings, start_outbox_worker=False)
    try:
        result = first.capture("Recall preserves durable restart work")
        assert result["processing_status"] == "pending"
        capture_id = result["id"]
    finally:
        first.close()

    second = MemoryPipeline(settings)
    second.gate_chain = GateFallbackChain([HeuristicBackend()])
    second.extractor = StubExtractor()
    try:
        deadline = time.time() + 2
        stored = None
        while time.time() < deadline:
            stored = second.get(capture_id)
            if stored and stored["processing_status"] == "complete":
                break
            time.sleep(0.01)
        assert stored is not None
        assert stored["processing_status"] == "complete"
        assert second.store.operational_stats()["outbox"] == {"complete": 1}
    finally:
        second.close()


class _FlakyEmbeddingProvider:
    model = "flaky"

    def __init__(self):
        self.calls = 0

    def embed(self, text: str) -> EmbeddingResult:
        self.calls += 1
        if self.calls == 1:
            raise EmbeddingError("temporary embedding failure")
        return EmbeddingResult([1.0, 0.0], self.model, "stable-content-hash")


def test_partial_derived_work_is_idempotent_on_retry(tmp_path):
    pipeline = _pipeline(tmp_path)
    provider = _FlakyEmbeddingProvider()
    pipeline.embedding_provider = provider
    gate_calls = 0
    classify = pipeline.gate_chain.classify

    def counted_classify(text):
        nonlocal gate_calls
        gate_calls += 1
        return classify(text)

    pipeline.gate_chain.classify = counted_classify
    try:
        result = pipeline.capture("Ema uses Python for Recall.", source="test")

        assert result["processing_status"] == "complete"
        assert provider.calls == 2
        assert gate_calls == 1
        with sqlite3.connect(pipeline.settings.DB_PATH) as conn:
            outbox = conn.execute(
                "SELECT status, attempts FROM outbox_jobs WHERE raw_capture_id = ?",
                (result["id"],),
            ).fetchone()
            links = conn.execute(
                "SELECT COUNT(*) FROM memory_entities WHERE memory_id = ?",
                (result["id"],),
            ).fetchone()[0]
            facts = conn.execute(
                """
                SELECT COUNT(*), COUNT(DISTINCT derivation_key)
                FROM facts WHERE derivation_key IS NOT NULL
                """
            ).fetchone()
        assert outbox == ("complete", 2)
        assert links >= 1
        assert facts[0] == facts[1]
        entity = pipeline.entity_store.get_entity("ema")
        assert entity["timeline"].count(f"memory {result['id']}") == 1
    finally:
        pipeline.close()


class _FailingExtractor(StubExtractor):
    def extract(self, *args, **kwargs):
        raise RuntimeError("permanent extraction failure")


def test_exhausted_worker_job_returns_failed_and_is_visible(tmp_path):
    pipeline = _pipeline(tmp_path, OUTBOX_MAX_ATTEMPTS=2)
    pipeline.extractor = _FailingExtractor()
    try:
        result = pipeline.capture("Recall must expose exhausted work")

        assert result["processing_status"] == "failed"
        job = pipeline.store.outbox_job(
            next(
                row["id"]
                for row in _outbox_rows(pipeline)
                if row["raw_capture_id"] == result["id"]
            )
        )
        assert job["status"] == "dead"
        assert job["attempts"] == 2
        assert "permanent extraction failure" in job["last_error"]
    finally:
        pipeline.close()


def _outbox_rows(pipeline: MemoryPipeline) -> list[dict]:
    with sqlite3.connect(pipeline.settings.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT * FROM outbox_jobs")]


def test_admin_status_and_single_dead_job_retry(tmp_path, capsys):
    settings = Settings(BASE_DIR=tmp_path, DB_PATH=tmp_path / "memory.db")
    store = MemoryStore(settings.DB_PATH)
    _capture_id, job_id = store.enqueue_capture("operator retry")
    job = store.claim_outbox_job(lease_seconds=10)
    assert job is not None
    store.fail_outbox_job(
        job,
        "dead for test",
        max_attempts=1,
        retry_base_seconds=1,
    )

    assert command_outbox(settings, Namespace(action="status", job_id=None)) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["counts"] == {"dead": 1}
    assert "content" not in status

    assert command_outbox(settings, Namespace(action="retry", job_id=job_id)) == 0
    retried = json.loads(capsys.readouterr().out)
    assert retried == {"job_id": job_id, "requeued": True}
