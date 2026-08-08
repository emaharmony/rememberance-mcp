"""Live JetStream verification, enabled only in the release environment."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

import pytest

from recall_mcp.config import Settings
from recall_mcp.extract import StubExtractor
from recall_mcp.gate_backends import GateFallbackChain, HeuristicBackend
from recall_mcp.nats_jetstream import DurableNatsSubscriber
from recall_mcp.pipeline import MemoryPipeline


pytestmark = pytest.mark.skipif(
    os.environ.get("RECALL_RUN_NATS_INTEGRATION") != "1",
    reason="requires a live JetStream server",
)


async def _publish(server: str, subject: str, payload: dict) -> None:
    import nats

    client = await nats.connect(server)
    try:
        await client.publish(subject, json.dumps(payload).encode("utf-8"))
        await client.flush()
    finally:
        await client.drain()


def test_jetstream_redelivers_and_deduplicates(tmp_path):
    suffix = uuid.uuid4().hex[:10]
    settings = Settings(
        BASE_DIR=tmp_path,
        DB_PATH=tmp_path / "memory.db",
        EMBEDDINGS_ENABLED=False,
        NATS_URL="nats://127.0.0.1:4222",
        NATS_SUBJECT=f"test.{suffix}.agent.output",
        NATS_STREAM=f"RECALL_TEST_{suffix.upper()}",
        NATS_CONSUMER=f"recall-test-{suffix}",
        NATS_DLQ_SUBJECT=f"test.{suffix}.agent.output.dlq",
    )
    pipeline = MemoryPipeline(settings)
    pipeline.gate_chain = GateFallbackChain([HeuristicBackend()])
    pipeline.extractor = StubExtractor()

    original_capture = pipeline.capture
    attempts = 0

    def flaky_capture(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("forced transient failure")
        return original_capture(*args, **kwargs)

    pipeline.capture = flaky_capture
    subscriber = DurableNatsSubscriber(pipeline, settings=settings)
    subscriber.start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not subscriber.health()["connected"]:
            time.sleep(0.1)
        assert subscriber.health()["connected"]

        payload = {
            "event_id": f"event-{suffix}",
            "content": "Important durable Recall architecture decision",
            "agent": "release-gate",
        }
        asyncio.run(_publish(settings.NATS_URL, settings.NATS_SUBJECT, payload))

        deadline = time.time() + 15
        while time.time() < deadline and subscriber.health()["processed"] < 1:
            time.sleep(0.2)
        health = subscriber.health()
        assert health["failed"] >= 1
        assert health["redelivered"] >= 1
        assert health["processed"] == 1
        assert pipeline.store.count() == 1

        asyncio.run(_publish(settings.NATS_URL, settings.NATS_SUBJECT, payload))
        time.sleep(1)
        assert pipeline.store.count() == 1
    finally:
        subscriber.stop()
        pipeline.close()
