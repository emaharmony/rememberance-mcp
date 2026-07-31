"""Durable, leased processing for Recall capture outbox jobs."""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from functools import partial
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from recall_mcp.store.store import MemoryStore, OutboxJob

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CaptureOutcome:
    """A processed capture ready for atomic outbox finalization."""

    result: dict
    memory_id: str | None
    raw_status: str
    gate_decision: str
    gate_confidence: float
    gate_backend: str
    gate_fallback_used: bool


class CaptureOutboxDispatcher:
    """Claim durable jobs and run them with bounded at-least-once delivery."""

    def __init__(
        self,
        *,
        store: MemoryStore,
        handler: Callable[[OutboxJob], CaptureOutcome],
        executor: ThreadPoolExecutor,
        capacity: threading.BoundedSemaphore,
        poll_interval: float,
        lease_seconds: float,
        max_attempts: int,
        retry_base_seconds: float,
    ) -> None:
        self.store = store
        self.handler = handler
        self.executor = executor
        self.capacity = capacity
        self.poll_interval = poll_interval
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.retry_base_seconds = retry_base_seconds
        self._running = False
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._state_lock = threading.Lock()
        self._waiters: dict[str, Future[dict]] = {}
        self._recent_results: OrderedDict[str, dict] = OrderedDict()
        self._inflight: set[str] = set()
        self._last_error = ""

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._dispatch_loop,
            daemon=True,
            name="recall-outbox-dispatcher",
        )
        self._thread.start()

    def stop(self, *, timeout: float = 10.0) -> None:
        self._running = False
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def notify(self) -> None:
        self._wake.set()

    def register_waiter(self, capture_id: str) -> Future[dict]:
        waiter: Future[dict] = Future()
        with self._state_lock:
            completed = self._recent_results.pop(capture_id, None)
            if completed is None:
                self._waiters[capture_id] = waiter
            else:
                waiter.set_result(completed)
        return waiter

    def abandon_waiter(self, capture_id: str, waiter: Future[dict]) -> None:
        with self._state_lock:
            if self._waiters.get(capture_id) is waiter:
                self._waiters.pop(capture_id, None)

    def health(self) -> dict[str, object]:
        with self._state_lock:
            inflight = len(self._inflight)
            last_error = self._last_error
        return {
            "running": self._running,
            "thread_alive": bool(self._thread and self._thread.is_alive()),
            "inflight": inflight,
            "last_error": last_error,
        }

    def _dispatch_loop(self) -> None:
        while self._running:
            if not self.capacity.acquire(blocking=False):
                self._wait()
                continue
            try:
                job = self.store.claim_outbox_job(lease_seconds=self.lease_seconds)
            except Exception as exc:
                self.capacity.release()
                self._record_error(exc)
                logger.exception("Unable to claim an outbox job")
                self._wait()
                continue
            if job is None:
                self.capacity.release()
                self._wait()
                continue

            with self._state_lock:
                self._inflight.add(job.id)
            try:
                future = self.executor.submit(self._run_job, job)
            except Exception as exc:
                with self._state_lock:
                    self._inflight.discard(job.id)
                self.capacity.release()
                self._record_error(exc)
                self._schedule_failure(job, exc)
                continue
            future.add_done_callback(partial(self._job_done, job.id))

    def _wait(self) -> None:
        self._wake.wait(self.poll_interval)
        self._wake.clear()

    def _run_job(self, job: OutboxJob) -> None:
        try:
            if job.event_type != "capture.process":
                raise ValueError(f"unsupported outbox event type: {job.event_type}")
            outcome = self.handler(job)
            if outcome.raw_status == "skipped":
                self.store.complete_skipped_outbox_job(
                    job,
                    gate_decision=outcome.gate_decision,
                    gate_confidence=outcome.gate_confidence,
                    gate_backend=outcome.gate_backend,
                    gate_fallback_used=outcome.gate_fallback_used,
                )
            elif outcome.memory_id is not None:
                self.store.complete_outbox_job(
                    job, outcome.memory_id, status=outcome.raw_status
                )
            else:
                raise RuntimeError("accepted capture outcome has no memory ID")
            self._resolve(job.raw_capture_id, outcome.result)
            with self._state_lock:
                self._last_error = ""
        except Exception as exc:
            self._record_error(exc)
            self._schedule_failure(job, exc)

    def _schedule_failure(self, job: OutboxJob, exc: Exception) -> None:
        try:
            status, delay = self.store.fail_outbox_job(
                job,
                str(exc),
                max_attempts=self.max_attempts,
                retry_base_seconds=self.retry_base_seconds,
            )
        except Exception as persistence_error:
            self._record_error(persistence_error)
            logger.exception("Unable to persist outbox failure for %s", job.id)
            return
        if status == "dead":
            self._resolve(
                job.raw_capture_id,
                {
                    "id": job.raw_capture_id,
                    "decision": "FAILED",
                    "confidence": job.gate_confidence,
                    "backend": job.gate_backend,
                    "fallback_used": bool(job.gate_fallback_used),
                    "category": job.requested_category,
                    "tier": job.requested_tier,
                    "summary": job.content[:200],
                    "topics": [],
                    "processing_status": "failed",
                    "processing_error": str(exc)[:1000],
                },
            )
            logger.error(
                "Outbox job %s exhausted after %s attempts", job.id, job.attempts
            )
        else:
            logger.warning(
                "Outbox job %s failed on attempt %s; retrying in %.2fs: %s",
                job.id,
                job.attempts,
                delay,
                exc,
            )
        self.notify()

    def _job_done(self, job_id: str, future: Future[None]) -> None:
        with self._state_lock:
            self._inflight.discard(job_id)
        self.capacity.release()
        try:
            future.result()
        except Exception as exc:  # pragma: no cover - _run_job contains failures
            self._record_error(exc)
            logger.exception("Unhandled outbox worker failure")
        self.notify()

    def _resolve(self, capture_id: str, result: dict) -> None:
        with self._state_lock:
            waiter = self._waiters.pop(capture_id, None)
            if waiter is None:
                self._recent_results[capture_id] = result
                self._recent_results.move_to_end(capture_id)
                while len(self._recent_results) > 128:
                    self._recent_results.popitem(last=False)
        if waiter is not None and not waiter.done():
            waiter.set_result(result)

    def _record_error(self, exc: Exception) -> None:
        with self._state_lock:
            self._last_error = str(exc)[:1000]
