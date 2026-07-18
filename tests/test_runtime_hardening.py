"""Focused tests for runtime validation and structured logging."""

from __future__ import annotations

import json
import logging
import sqlite3

import pytest

from recall_mcp.config import Settings
from recall_mcp.logging_config import JsonFormatter, safe_endpoint
from recall_mcp.pipeline import MemoryPipeline


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"READS_PER_MINUTE": 0}, "rate limits"),
        ({"WRITES_PER_MINUTE": 0}, "rate limits"),
        ({"CAPTURE_PROCESSING_TIMEOUT": 0}, "PROCESSING"),
        ({"OLLAMA_TIMEOUT_SECONDS": 0}, "OLLAMA_TIMEOUT"),
        ({"NATS_URL": "http://127.0.0.1:4222"}, "NATS URL"),
        ({"OLLAMA_BASE_URL": "file:///tmp/ollama"}, "HTTP"),
        ({"PROCESSING_QUEUE_LIMIT": 1}, "queue limit"),
    ],
)
def test_settings_reject_invalid_runtime_limits(tmp_path, override, message):
    with pytest.raises(ValueError, match=message):
        Settings(BASE_DIR=tmp_path, **override)


def test_json_logging_and_endpoint_redaction():
    record = logging.LogRecord(
        "recall.test", logging.INFO, __file__, 1, "ready %s", ("now",), None
    )
    payload = json.loads(JsonFormatter().format(record))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "recall.test"
    assert payload["message"] == "ready now"
    assert "+00:00" in payload["timestamp"]
    assert safe_endpoint("nats://user:secret@127.0.0.1:4222") == (
        "nats://127.0.0.1:4222"
    )


class _NoCapacity:
    def acquire(self, *, blocking):
        assert blocking is False
        return False


def test_capture_fails_safely_when_processing_queue_is_full(tmp_path):
    settings = Settings(
        BASE_DIR=tmp_path,
        DB_PATH=tmp_path / "memory.db",
        EMBEDDINGS_ENABLED=False,
    )
    pipeline = MemoryPipeline(settings)
    pipeline._processing_slots = _NoCapacity()

    result = pipeline.capture("preserve this raw capture")

    assert result["processing_status"] == "failed"
    assert result["processing_error"] == "processing capacity exhausted"
    with sqlite3.connect(settings.DB_PATH) as connection:
        row = connection.execute(
            "SELECT status, error FROM raw_captures WHERE id = ?",
            (result["id"],),
        ).fetchone()
    assert row == ("failed", "processing capacity exhausted")
