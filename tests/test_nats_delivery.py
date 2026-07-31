"""Unit coverage for durable JetStream acknowledgement behavior."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from recall_mcp.config import Settings
from recall_mcp.nats_jetstream import DurableNatsSubscriber


class _Store:
    def __init__(self, reserve: bool = True):
        self.reserve = reserve
        self.completed = []
        self.failed = []

    def reserve_event(self, event_id):
        self.event_id = event_id
        return self.reserve

    def complete_event(self, event_id, memory_id):
        self.completed.append((event_id, memory_id))

    def fail_event(self, event_id, error):
        self.failed.append((event_id, error))


class _Pipeline:
    def __init__(
        self, settings, *, reserve=True, error=None, processing_status="complete"
    ):
        self.settings = settings
        self.store = _Store(reserve)
        self.error = error
        self.processing_status = processing_status
        self.captures = []

    def capture(self, content, **kwargs):
        self.captures.append((content, kwargs))
        if self.error:
            raise RuntimeError(self.error)
        return {"id": "memory-1", "processing_status": self.processing_status}


class _Message:
    def __init__(self, data, *, delivered=1):
        self.data = data
        self.subject = "codex.agent.output"
        self.metadata = SimpleNamespace(num_delivered=delivered)
        self.acked = 0
        self.naks = []

    async def ack(self):
        self.acked += 1

    async def nak(self, *, delay):
        self.naks.append(delay)


class _JetStream:
    def __init__(self):
        self.published = []

    async def publish(self, subject, payload):
        self.published.append((subject, json.loads(payload)))


def _subscriber(tmp_path, **pipeline_kwargs):
    settings = Settings(BASE_DIR=tmp_path, DB_PATH=tmp_path / "memory.db")
    pipeline = _Pipeline(settings, **pipeline_kwargs)
    return DurableNatsSubscriber(pipeline, settings=settings), pipeline


def test_invalid_nats_payload_is_dead_lettered(tmp_path):
    subscriber, _pipeline = _subscriber(tmp_path)
    jetstream = _JetStream()
    message = _Message(b"not-json")

    asyncio.run(subscriber._handle_message(jetstream, message))

    assert message.acked == 1
    assert subscriber.health()["dead_lettered"] == 1
    assert jetstream.published[0][0] == subscriber.settings.NATS_DLQ_SUBJECT
    assert "payload" in jetstream.published[0][1]


def test_nats_acknowledges_only_after_event_completion(tmp_path):
    subscriber, pipeline = _subscriber(tmp_path)
    jetstream = _JetStream()
    message = _Message(
        json.dumps(
            {
                "event_id": "event-1",
                "content": "durable memory",
                "agent": "codex",
                "project": "recall",
            }
        ).encode()
    )

    asyncio.run(subscriber._handle_message(jetstream, message))

    assert pipeline.store.completed == [("event-1", "memory-1")]
    assert pipeline.captures[0][1]["agent"] == "codex"
    assert message.acked == 1
    assert message.naks == []
    assert subscriber.health()["processed"] == 1


def test_nats_acknowledges_a_durably_pending_capture(tmp_path):
    subscriber, pipeline = _subscriber(tmp_path, processing_status="pending")
    message = _Message(b'{"event_id":"pending","content":"durable pending"}')

    asyncio.run(subscriber._handle_message(_JetStream(), message))

    assert pipeline.store.completed == [("pending", "memory-1")]
    assert message.acked == 1
    assert message.naks == []


def test_duplicate_nats_event_is_acked_without_recapture(tmp_path):
    subscriber, pipeline = _subscriber(tmp_path, reserve=False)
    message = _Message(b'{"event_id":"duplicate","content":"already stored"}')

    asyncio.run(subscriber._handle_message(_JetStream(), message))

    assert message.acked == 1
    assert pipeline.captures == []


def test_transient_nats_failure_is_nacked_for_redelivery(tmp_path):
    subscriber, pipeline = _subscriber(tmp_path, error="temporary")
    message = _Message(b'{"event_id":"retry","content":"try again"}', delivered=2)

    asyncio.run(subscriber._handle_message(_JetStream(), message))

    assert message.acked == 0
    assert message.naks == [4]
    assert pipeline.store.failed[0][0] == "retry"
    assert subscriber.health()["failed"] == 1
    assert subscriber.health()["redelivered"] == 1


def test_tenth_nats_failure_is_dead_lettered(tmp_path):
    subscriber, pipeline = _subscriber(tmp_path, error="exhausted")
    jetstream = _JetStream()
    message = _Message(b'{"event_id":"poison","content":"bad event"}', delivered=10)

    asyncio.run(subscriber._handle_message(jetstream, message))

    assert pipeline.store.failed[0][0] == "poison"
    assert message.acked == 1
    assert message.naks == []
    assert jetstream.published[0][0] == subscriber.settings.NATS_DLQ_SUBJECT
