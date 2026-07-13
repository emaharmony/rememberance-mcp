"""Golden-path integration tests for the REST surface and the Prism /v1 layer.

These exercise the full HTTP path end-to-end against a real (temp-isolated)
pipeline. They are deliberately broad: a single run covers capture, keyword
search, the /v1 compatibility endpoints, and the /health self-probe.

They guard the failure modes that previously slipped through unit tests:
  - the FTS5 index silently returning nothing (external-content drift),
  - /v1/context/build returning empty context_markdown,
  - the Prism CaptureRequest shape not mapping onto the pipeline.

Isolation: REMEMBRANCE_HOME points at a tmp dir, so the gate falls back to the
heuristic backend (no DilBERT model needed) and no real data is touched.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest


@pytest.fixture()
def server():
    """Start the REST API on an ephemeral port against an isolated pipeline.

    Uses tempfile.mkdtemp (not pytest's tmp_path) so it runs even where the
    pytest temp factory can't scan its base dir. REMEMBRANCE_HOME isolation
    means the gate falls back to heuristic — no torch/model needed.
    """
    home = tempfile.mkdtemp(prefix="remembrance-test-")
    saved = {k: os.environ.get(k) for k in ("REMEMBRANCE_HOME", "REMEMBRANCE_GATE_BACKENDS")}
    os.environ["REMEMBRANCE_HOME"] = home
    os.environ["REMEMBRANCE_GATE_BACKENDS"] = "heuristic"

    from remembrance_mcp.api.rest import RemembranceHandler
    from remembrance_mcp.config import Settings
    from remembrance_mcp.pipeline import MemoryPipeline

    pipeline = MemoryPipeline(settings=Settings())
    RemembranceHandler.pipeline = pipeline

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), RemembranceHandler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(home, ignore_errors=True)


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def _post(base, path, body):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        base + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def test_capture_then_search_finds_it(server):
    """The core golden path: a captured memory must be retrievable by keyword.

    This is exactly the FTS5 regression — if the index isn't populated, search
    returns nothing even though the memory was stored.
    """
    token = "zzqwxmarker"
    status, res = _post(
        server,
        "/capture",
        {
            "text": f"We decided the {token} subsystem is the canonical event bus.",
            "source": "test",
        },
    )
    assert status == 201
    assert res["decision"] != "SKIP"
    assert res["id"]

    status, res = _get(server, f"/search?q={token}&mode=keyword&limit=5")
    assert status == 200
    assert res["count"] >= 1
    assert any(token in (m.get("content") or "") for m in res["results"])


def test_v1_context_build_returns_markdown(server):
    """POST /v1/context/build must return non-empty context_markdown after a
    relevant capture (the shape Prism injects)."""
    token = "qprojmarker"
    _post(
        server,
        "/capture",
        {
            "text": f"Important decision: {token} is the shared memory brain for all agents.",
            "source": "test",
        },
    )
    status, res = _post(
        server,
        "/v1/context/build",
        {
            "task": token,
            "project_id": "test",
            "agent_id": "pytest",
            "max_tokens": 1500,
        },
    )
    assert status == 200
    assert res["context_markdown"].strip()
    assert token in res["context_markdown"]
    assert res["selected_memories"]


def test_v1_memory_ingest_prism_shape(server):
    """Prism's CaptureRequest (content/source_agent/scope/...) must map onto capture."""
    token = "ingestmarker"
    status, res = _post(
        server,
        "/v1/memory/ingest",
        {
            "content": f"We decided {token} ships in v3.",
            "source_agent": "prism:astraea",
            "category": "decision",
            "scope": "project",
            "project_id": "prism",
            "title": "decision",
        },
    )
    assert status == 201
    assert res["decision"] != "SKIP"


def test_v1_health_reports_fts_ok(server):
    """/v1/health must report fts_ok True once a memory exists and is indexed."""
    _post(server, "/capture", {"text": "We decided healthmarker is persisted.", "source": "test"})
    status, res = _get(server, "/v1/health")
    assert status == 200
    assert res["status"] == "ok"
    assert res["memories"] >= 1
    assert res["fts_ok"] is True
