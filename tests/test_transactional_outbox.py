"""Behavior coverage for the Phase 0 transactional capture outbox."""

from __future__ import annotations

import json
import sqlite3
import time
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest

from recall_mcp.admin import command_doctor, command_outbox
from recall_mcp.config import Settings
from recall_mcp.embeddings import EmbeddingResult
from recall_mcp.extract import StubExtractor
from recall_mcp.gate_backends import GateFallbackChain, HeuristicBackend
from recall_mcp.pipeline import MemoryPipeline
from recall_mcp.store import MemoryStore
from recall_mcp.store.migrations import MIGRATIONS, run_migrations


OUTBOX_ZERO_COUNTS = {
    "pending": 0,
    "processing": 0,
    "retry": 0,
    "complete": 0,
    "dead": 0,
}


def _outbox_counts(**overrides: int) -> dict[str, int]:
    return {**OUTBOX_ZERO_COUNTS, **overrides}


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

    assert [migration.version for migration in result.applied] == [
        5,
        6,
        7,
        8,
        9,
        10,
        11,
    ]
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


def test_competing_dispatchers_execute_one_claimed_job_once(tmp_path):
    settings = Settings(
        BASE_DIR=tmp_path,
        DB_PATH=tmp_path / "memory.db",
        EMBEDDINGS_ENABLED=False,
        OUTBOX_POLL_INTERVAL=0.01,
    )
    pipelines = [
        MemoryPipeline(settings, start_outbox_worker=False),
        MemoryPipeline(settings, start_outbox_worker=False),
    ]
    calls = 0
    calls_lock = Lock()

    for pipeline in pipelines:
        pipeline.gate_chain = GateFallbackChain([HeuristicBackend()])
        pipeline.extractor = StubExtractor()
        handler = pipeline.outbox_dispatcher.handler

        def counted_handler(job, handler=handler):
            nonlocal calls
            with calls_lock:
                calls += 1
            return handler(job)

        pipeline.outbox_dispatcher.handler = counted_handler
        pipeline.outbox_dispatcher.start()

    try:
        capture_id, job_id = pipelines[0].store.enqueue_capture(
            "dispatch this durable job once"
        )
        for pipeline in pipelines:
            pipeline.outbox_dispatcher.notify()

        deadline = time.time() + 2
        job = None
        while time.time() < deadline:
            job = pipelines[0].store.outbox_job(job_id)
            if job and job["status"] == "complete":
                break
            time.sleep(0.01)

        assert job is not None
        assert job["status"] == "complete"
        assert job["attempts"] == 1
        assert calls == 1
        assert pipelines[0].get(capture_id)["processing_status"] == "complete"
    finally:
        for pipeline in pipelines:
            pipeline.close()


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


def test_retry_backoff_is_exponential_and_bounded_at_300_seconds(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    _capture_id, job_id = store.enqueue_capture("retry with bounded backoff")
    now = time.time() + 1

    for expected_delay in (2, 4, 8, 16, 32, 64, 128, 256, 300):
        job = store.claim_outbox_job(lease_seconds=10, now=now)
        assert job is not None
        status, delay = store.fail_outbox_job(
            job,
            "safe test failure",
            max_attempts=10,
            retry_base_seconds=2,
            now=now,
        )
        assert (status, delay) == ("retry", expected_delay)
        now += expected_delay

    terminal = store.claim_outbox_job(lease_seconds=10, now=now)
    assert terminal is not None
    assert terminal.attempts == 10
    assert store.fail_outbox_job(
        terminal,
        "safe terminal failure",
        max_attempts=10,
        retry_base_seconds=2,
        now=now,
    ) == ("dead", 0)
    assert store.outbox_job(job_id)["status"] == "dead"


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
        outbox_counts = None
        while time.time() < deadline:
            stored = second.get(capture_id)
            outbox_counts = second.store.operational_stats()["outbox"]
            if (
                stored
                and stored["processing_status"] == "complete"
                and outbox_counts == _outbox_counts(complete=1)
            ):
                break
            time.sleep(0.01)
        assert stored is not None
        assert stored["processing_status"] == "complete"
        assert outbox_counts == _outbox_counts(complete=1)
    finally:
        second.close()


class _StableEmbeddingProvider:
    model = "stable"

    def __init__(self):
        self.calls = 0

    def embed(self, text: str) -> EmbeddingResult:
        self.calls += 1
        return EmbeddingResult([1.0, 0.0], self.model, "stable-content-hash")


@pytest.mark.parametrize("failure_stage", ["memory", "graph", "fact", "embedding"])
def test_partial_derived_work_is_idempotent_on_retry(
    tmp_path, monkeypatch, caplog, failure_stage
):
    pipeline = _pipeline(tmp_path)
    provider = _StableEmbeddingProvider()
    pipeline.embedding_provider = provider
    gate_calls = 0
    classify = pipeline.gate_chain.classify
    failed = False
    failure_lock = Lock()
    secret_marker = "customer-secret-raw-capture"

    def counted_classify(text):
        nonlocal gate_calls
        gate_calls += 1
        return classify(text)

    target, attribute = {
        "memory": (pipeline.store, "ensure_capture_memory"),
        "graph": (pipeline.graph_wiring, "wire"),
        "fact": (pipeline, "_capture_facts"),
        "embedding": (pipeline, "_embed_memory"),
    }[failure_stage]
    operation = getattr(target, attribute)

    def fail_after_write(*args, **kwargs):
        nonlocal failed
        result = operation(*args, **kwargs)
        with failure_lock:
            if not failed:
                failed = True
                raise RuntimeError(f"{secret_marker} after {failure_stage}")
        return result

    pipeline.gate_chain.classify = counted_classify
    monkeypatch.setattr(target, attribute, fail_after_write)
    try:
        result = pipeline.capture("Ema uses Python for Recall.", source="test")

        assert result["processing_status"] == "complete"
        assert provider.calls == (2 if failure_stage == "embedding" else 1)
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
            memories = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE id = ?", (result["id"],)
            ).fetchone()[0]
            embedding = conn.execute(
                """
                SELECT embedding_status, embedding_model
                FROM memories WHERE id = ?
                """,
                (result["id"],),
            ).fetchone()
        assert outbox == ("complete", 2)
        assert memories == 1
        assert links >= 1
        assert facts[0] >= 1
        assert facts[0] == facts[1]
        assert embedding == ("ready", "stable")
        entity = pipeline.entity_store.get_entity("ema")
        assert entity["timeline"].count(f"memory {result['id']}") == 1
        assert secret_marker not in caplog.text
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
        assert job["last_error"] == "RuntimeError: capture processing failed"
        assert "permanent extraction failure" not in job["last_error"]
        assert result["processing_error"] == job["last_error"]
        assert pipeline.outbox_dispatcher.health()["last_error"] == job["last_error"]
        with sqlite3.connect(pipeline.settings.DB_PATH) as conn:
            raw_error = conn.execute(
                "SELECT error FROM raw_captures WHERE id = ?", (result["id"],)
            ).fetchone()[0]
            memory_error = conn.execute(
                "SELECT processing_error FROM memories WHERE id = ?", (result["id"],)
            ).fetchone()[0]
        assert raw_error == job["last_error"]
        assert memory_error == job["last_error"]
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
    assert status["counts"] == _outbox_counts(dead=1)
    assert "content" not in status

    assert command_outbox(settings, Namespace(action="retry", job_id=job_id)) == 0
    retried = json.loads(capsys.readouterr().out)
    assert retried == {"job_id": job_id, "requeued": True}


def test_doctor_reports_workerless_outbox_observability(tmp_path, capsys, monkeypatch):
    settings = Settings(BASE_DIR=tmp_path, DB_PATH=tmp_path / "memory.db")
    MemoryStore(settings.DB_PATH)

    class _HealthyResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(
        "recall_mcp.admin.urllib.request.urlopen",
        lambda *_args, **_kwargs: _HealthyResponse(),
    )

    assert command_doctor(settings, Namespace(token_file=None)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["outbox"] == OUTBOX_ZERO_COUNTS
    assert result["outbox_active_leases"] == 0
    assert result["outbox_oldest_due_seconds"] == 0
    assert result["outbox_worker"] == {
        "observed": False,
        "running": False,
        "thread_alive": False,
        "inflight": 0,
        "last_error": None,
    }
    assert result["retrieval_feedback"]["policy_version"] == "utility-v1"
    assert result["retrieval_feedback"]["retrieval_runs"] == 0
    assert result["retrieval_feedback"]["last_error"] == ""
    assert result["context_service"]["available"] is True
    assert result["context_service"]["migration_available"] is True
    assert result["context_service"]["feedback_linkage_healthy"] is True
    assert result["context_service"]["policy_version"] == "context-v2"
    assert result["context_service"]["token_estimator_version"] == "chars-v1"


def test_pipeline_close_is_idempotent_and_stops_dispatcher(tmp_path):
    pipeline = _pipeline(tmp_path)
    dispatcher = pipeline.outbox_dispatcher

    assert dispatcher.health()["thread_alive"] is True
    pipeline.close()
    pipeline.close()

    assert dispatcher.health()["running"] is False
    assert dispatcher.health()["thread_alive"] is False
    with pytest.raises(RuntimeError, match="memory pipeline is closed"):
        pipeline.capture("must not enqueue after shutdown")

    managed_settings = Settings(
        BASE_DIR=tmp_path / "managed",
        DB_PATH=tmp_path / "managed" / "memory.db",
        EMBEDDINGS_ENABLED=False,
    )
    with MemoryPipeline(managed_settings) as managed:
        managed_dispatcher = managed.outbox_dispatcher
        assert managed_dispatcher.health()["thread_alive"] is True
    assert managed_dispatcher.health()["thread_alive"] is False
