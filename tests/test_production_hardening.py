"""Regression coverage for Recall 2.1 production hardening."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from argparse import Namespace
from http.server import HTTPServer
from pathlib import Path
from threading import Thread

import pytest

from recall_mcp.admin import command_backup, command_restore
from recall_mcp.api.rest import RecallHandler
from recall_mcp.api.security import TokenAuthenticator
from recall_mcp.config import Settings
from recall_mcp.embeddings import EmbeddingResult
from recall_mcp.extract import OllamaExtractor, StubExtractor
from recall_mcp.gate_backends import GateFallbackChain, HeuristicBackend
from recall_mcp.nats_jetstream import DurableNatsSubscriber
from recall_mcp.pipeline import MemoryPipeline
from recall_mcp.store import MemoryStore


_PIPELINES: list[MemoryPipeline] = []


@pytest.fixture(autouse=True)
def close_test_pipelines(tmp_path):
    """Release dispatcher threads before pytest removes temporary databases."""
    yield
    while _PIPELINES:
        _PIPELINES.pop().close()


class FakeEmbeddingProvider:
    model = "fake-embedding"

    def embed(self, text: str) -> EmbeddingResult:
        lowered = text.lower()
        vector = [1.0, 0.0] if "apple" in lowered else [0.0, 1.0]
        import hashlib

        return EmbeddingResult(
            vector,
            self.model,
            hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )

    def embed_batch(self, texts: list[str]) -> list[EmbeddingResult]:
        return [self.embed(text) for text in texts]


def make_pipeline(tmp_path: Path, **settings_overrides) -> MemoryPipeline:
    settings = Settings(
        DB_PATH=tmp_path / "memory.db",
        BASE_DIR=tmp_path,
        EMBEDDINGS_ENABLED=False,
        **settings_overrides,
    )
    pipeline = MemoryPipeline(settings)
    _PIPELINES.append(pipeline)
    pipeline.gate_chain = GateFallbackChain([HeuristicBackend()])
    pipeline.extractor = StubExtractor()
    return pipeline


def request_json(
    url: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    token: str | None = None,
) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


@pytest.fixture
def secured_server(tmp_path):
    pipeline = make_pipeline(tmp_path, API_TOKEN="test-secret")
    RecallHandler.pipeline = pipeline
    RecallHandler.rate_limiter = None
    server = HTTPServer(("127.0.0.1", 0), RecallHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", pipeline
    finally:
        server.shutdown()
        server.server_close()


def test_settings_read_environment_and_reject_open_bind(monkeypatch, tmp_path):
    monkeypatch.setenv("RECALL_HOME", str(tmp_path))
    monkeypatch.setenv("RECALL_PORT", "9001")
    monkeypatch.setenv("RECALL_MAX_RESULTS", "42")
    settings = Settings()
    assert settings.PORT == 9001
    assert settings.MAX_RESULTS == 42

    with pytest.raises(ValueError, match="non-loopback"):
        Settings(BASE_DIR=tmp_path, HOST="0.0.0.0")


def test_preference_category_is_valid():
    assert OllamaExtractor._validate_category("preference") == "preference"


def test_consolidation_requires_five_explicit_accesses(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    memory_id = store.store(
        "Important active work",
        "Important active work",
        "project",
        "active",
        [],
    )
    assert store.consolidate()["promoted"] == 0
    for _ in range(5):
        assert store.get(memory_id) is not None
    # Retrieval count alone does not change durable importance in Phase 2.
    assert store.consolidate()["promoted"] == 0


def test_scoped_search_includes_global_but_not_other_scope(tmp_path):
    pipeline = make_pipeline(tmp_path)
    alpha = pipeline.capture("Shared marker apple alpha", project="alpha")
    beta = pipeline.capture("Shared marker apple beta", project="beta")
    global_memory = pipeline.capture("Shared marker apple global")

    results = pipeline.hybrid_search.search(
        "Shared marker apple",
        mode="keyword",
        project="alpha",
        limit=20,
    )
    ids = {result["id"] for result in results}
    assert alpha["id"] in ids
    assert global_memory["id"] in ids
    assert beta["id"] not in ids


def test_vector_and_balanced_search_use_embedding_provider(tmp_path):
    pipeline = make_pipeline(tmp_path)
    apple = pipeline.capture("apple architecture")
    banana = pipeline.capture("banana architecture")
    provider = FakeEmbeddingProvider()
    pipeline.embedding_provider = provider
    pipeline.hybrid_search.embedding_provider = provider
    assert pipeline._embed_memory(apple["id"], "apple architecture")
    assert pipeline._embed_memory(banana["id"], "banana architecture")

    vector = pipeline.hybrid_search.search("apple query", mode="vector")
    balanced = pipeline.hybrid_search.search("apple", mode="balanced")
    assert vector[0]["id"] == apple["id"]
    assert {"vector", "sqlite-vec"} & set(vector[0]["sources"])
    assert any({"vector", "sqlite-vec"} & set(result["sources"]) for result in balanced)


def test_capture_connects_structured_facts(tmp_path):
    pipeline = make_pipeline(tmp_path)
    result = pipeline.capture("Ema uses Python for Recall.", source="test")
    facts = pipeline.fact_store.get_entity_facts("ema")
    assert result["facts_created"] >= 1
    assert any(
        fact["claim_key"] == "uses" and "Python" in fact["claim_value"]
        for fact in facts
    )


def test_event_reservations_are_database_backed(tmp_path):
    pipeline = make_pipeline(tmp_path)
    assert pipeline.store.reserve_event("event-1")
    assert not pipeline.store.reserve_event("event-1")
    pipeline.store.fail_event("event-1", "temporary")
    assert pipeline.store.reserve_event("event-1")
    pipeline.store.complete_event("event-1", None)
    assert not pipeline.store.reserve_event("event-1")


def test_nats_event_id_is_stable():
    payload = {"agent": "codex", "session_id": "s1", "turn": 2}
    first = DurableNatsSubscriber._event_id(payload, b'{"content":"hello"}')
    second = DurableNatsSubscriber._event_id(payload, b'{"content":"hello"}')
    assert first == second
    assert first.startswith("nats_")


def test_secured_rest_requires_auth_and_supports_delete(secured_server):
    base_url, _pipeline = secured_server
    status, _ = request_json(f"{base_url}/health/live")
    assert status == 200
    status, _ = request_json(f"{base_url}/stats")
    assert status == 401

    status, captured = request_json(
        f"{base_url}/capture",
        method="POST",
        token="test-secret",
        body={"text": "Recall security decision", "project": "recall"},
    )
    assert status == 201
    assert captured["processing_status"] == "complete"

    status, deleted = request_json(
        f"{base_url}/memories/{captured['id']}",
        method="DELETE",
        token="test-secret",
    )
    assert status == 200
    assert deleted["deleted"] is True


def test_secured_rest_context_metrics_and_safe_errors(secured_server):
    base_url, _pipeline = secured_server
    request_json(
        f"{base_url}/capture",
        method="POST",
        token="test-secret",
        body={"text": "Context marker for secure endpoint"},
    )
    status, context = request_json(
        f"{base_url}/context/build",
        method="POST",
        token="test-secret",
        body={"task": "Context marker"},
    )
    assert status == 200
    assert context["memories"]

    request = urllib.request.Request(
        f"{base_url}/metrics",
        headers={"Authorization": "Bearer test-secret"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        metrics = response.read().decode()
    assert "recall_memories" in metrics
    assert "recall_outbox_complete" in metrics
    assert "recall_outbox_worker_alive 1.0" in metrics
    assert "recall_outbox_dispatcher_error 0.0" in metrics

    status, error = request_json(
        f"{base_url}/search?q=x&limit=100000",
        token="test-secret",
    )
    assert status == 400
    assert "between" in error["error"]


def test_previous_token_expires_after_rotation_window(tmp_path):
    current = tmp_path / "current"
    previous = tmp_path / "previous"
    current.write_text("current-token", encoding="utf-8")
    previous.write_text("previous-token", encoding="utf-8")
    settings = Settings(
        BASE_DIR=tmp_path,
        API_TOKEN_FILE=current,
        PREVIOUS_API_TOKEN_FILE=previous,
    )
    auth = TokenAuthenticator(settings)
    assert auth.validate("Bearer current-token")
    assert auth.validate("Bearer previous-token")

    old = time.time() - 25 * 3600
    os.utime(previous, (old, old))
    assert not auth.validate("Bearer previous-token")


def test_backup_and_restore_round_trip(tmp_path):
    source_home = tmp_path / "source"
    source = make_pipeline(source_home)
    captured = source.capture("Backup marker for Recall")
    backup_dir = tmp_path / "backup"

    assert command_backup(source.settings, Namespace(destination=backup_dir)) == 0
    restored_home = tmp_path / "restored"
    restored_settings = Settings(
        BASE_DIR=restored_home,
        DB_PATH=restored_home / "memory.db",
        EMBEDDINGS_ENABLED=False,
    )
    assert (
        command_restore(
            restored_settings,
            Namespace(source=backup_dir, force=True),
        )
        == 0
    )
    restored = MemoryPipeline(restored_settings)
    _PIPELINES.append(restored)
    assert restored.get(captured["id"]) is not None


def test_dream_reembeds_stale_content(tmp_path):
    pipeline = make_pipeline(tmp_path)
    captured = pipeline.capture("apple stale embedding")
    provider = FakeEmbeddingProvider()
    pipeline.dream_cycle.embedding_provider = provider

    report = pipeline.dream(phases=["embed_stale"])
    assert report["totals"]["embeddings_refreshed"] == 1
    stored = pipeline.get(captured["id"])
    assert stored["embedding_status"] == "ready"
    assert stored["embedding_model"] == provider.model


class SlowExtractor(StubExtractor):
    def extract(self, *args, **kwargs):
        time.sleep(0.05)
        return super().extract(*args, **kwargs)


def test_capture_persists_raw_before_async_processing(tmp_path):
    pipeline = make_pipeline(tmp_path, CAPTURE_PROCESSING_TIMEOUT=0.001)
    pipeline.extractor = SlowExtractor()

    result = pipeline.capture(
        "Important Recall architecture decision with durable raw processing"
    )
    assert result["processing_status"] == "pending"
    assert result["id"]

    with sqlite3.connect(str(pipeline.settings.DB_PATH)) as connection:
        raw = connection.execute(
            "SELECT content, status FROM raw_captures WHERE id = ?",
            (result["id"],),
        ).fetchone()
    assert raw is not None
    assert raw[0].startswith("Important Recall")

    deadline = time.time() + 2
    stored = None
    while time.time() < deadline:
        stored = pipeline.get(result["id"])
        if stored is None or stored["processing_status"] == "pending":
            time.sleep(0.02)
        else:
            break
    assert stored is not None
    assert stored["processing_status"] == "complete"


def test_default_previous_token_file_has_rotation_overlap(tmp_path):
    current = tmp_path / "api-token"
    previous = tmp_path / "api-token.previous"
    current.write_text("current-token", encoding="utf-8")
    previous.write_text("previous-token", encoding="utf-8")
    settings = Settings(BASE_DIR=tmp_path, API_TOKEN_FILE=current)
    auth = TokenAuthenticator(settings)
    assert auth.validate("Bearer previous-token")


def test_memory_delete_cleans_graph_links(tmp_path):
    pipeline = make_pipeline(tmp_path)
    captured = pipeline.capture("Ema uses Python for Recall.")
    with sqlite3.connect(str(pipeline.settings.DB_PATH)) as connection:
        before = connection.execute(
            "SELECT COUNT(*) FROM memory_entities WHERE memory_id = ?",
            (captured["id"],),
        ).fetchone()[0]
    assert before >= 1
    assert pipeline.delete(captured["id"])
    with sqlite3.connect(str(pipeline.settings.DB_PATH)) as connection:
        after = connection.execute(
            "SELECT COUNT(*) FROM memory_entities WHERE memory_id = ?",
            (captured["id"],),
        ).fetchone()[0]
    assert after == 0
