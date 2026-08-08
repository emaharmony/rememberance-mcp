"""Tests for the Codex SessionStart hook's Phase 6 CAG integration.

Loads `src/recall_mcp/integrations/codex/recall_session.py` the same way
tests/test_inject_context_cag.py loads the Claude Code hook (importlib, spec
-> module_from_spec -> exec_module), so these tests exercise the actual hook
file rather than a reimplementation of its logic.

Most tests stub `urllib.request.urlopen` to control the CAG delivery /
task-bootstrap / v1-fallback responses deterministically and fast. One test
(`test_real_service_end_to_end_delivery`) instead starts a REAL REST server
backed by a real CAGDeliveryService (mirroring the `api_server` fixture in
tests/test_rest_api.py) and drives the hook against it over actual HTTP, with
no stubbing -- this is what proves the request bodies the hook builds
actually satisfy the server's own validation, not just a hand-rolled stub of
what we assume it accepts.

This file intentionally duplicates the small fake-urlopen/delivery-fixture
helpers from tests/test_inject_context_cag.py rather than importing them,
since that module belongs to a different owner and this file should not
become coupled to its internal shape.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

HOOK_PATH = (
    Path(__file__).parents[1]
    / "src"
    / "recall_mcp"
    / "integrations"
    / "codex"
    / "recall_session.py"
)

OLD_SHIM_HOOK_PATH = (
    Path(__file__).parents[1] / "integrations" / "codex" / "recall_session.py"
)

CLAUDE_HOOK_PATH = (
    Path(__file__).parents[1]
    / "src"
    / "recall_mcp"
    / "integrations"
    / "claude_code"
    / "inject_context.py"
)


def _load_hook(path=HOOK_PATH, name="recall_codex_session_cag_under_test"):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _set_scope_env(
    monkeypatch, *, user="u1", workspace="w1", project="p1", repository="r1"
):
    monkeypatch.setenv("RECALL_USER_ID", user)
    monkeypatch.setenv("RECALL_WORKSPACE_ID", workspace)
    monkeypatch.setenv("RECALL_PROJECT_ID", project)
    monkeypatch.setenv("RECALL_REPOSITORY_ID", repository)


class _FakeHTTPResponse:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _make_fake_urlopen(rules, calls):
    """Build a stand-in for `urllib.request.urlopen`.

    `rules` maps a URL substring to either a JSON-serializable payload, a
    callable(body_dict) -> payload, or an Exception instance to raise.
    `calls` is appended with (url, parsed_body_or_None) for every request, so
    tests can assert on what was actually sent and in what order.
    """

    def _fake(request, timeout=None):
        if isinstance(request, str):
            url = request
            body = None
        else:
            url = request.full_url
            raw = getattr(request, "data", None)
            body = json.loads(raw.decode("utf-8")) if raw else None
        calls.append((url, body))
        for substring, rule in rules.items():
            if substring in url:
                if isinstance(rule, Exception):
                    raise rule
                if callable(rule):
                    return _FakeHTTPResponse(rule(body))
                return _FakeHTTPResponse(rule)
        raise AssertionError(f"unexpected URL requested: {url}")

    return _fake


def _run_hook(module, monkeypatch, event, fake_urlopen):
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    return module.main()


def _full_delivery(
    *, context_pack_id="context-1", fingerprint_char="a", body_text="Body."
):
    return {
        "delivery_mode": "full",
        "context": {
            "full": {"inline_context": f"## Evidence 1 (mem-1)\n{body_text}"},
            "delta": None,
            "mandatory_refresh": [],
        },
        "skills": {
            "unchanged": [],
            "updated": [],
            "missing": [],
            "stale_client_versions": [],
            "removed": [],
        },
        "handoffs": {
            "unchanged": [],
            "updated": [],
            "missing": [],
            "refresh_required": [],
        },
        "authoritative_state": {
            "checkpoint_version": 0,
            "context_pack_id": context_pack_id,
            "context_pack_fingerprint": "sha256:" + fingerprint_char * 64,
            "skills": {},
            "handoffs": {},
        },
        "warnings": [],
    }


def _no_change_delivery(*, context_pack_id="context-1", fingerprint_char="a"):
    return {
        "delivery_mode": "no_change",
        "context": {
            "full": None,
            "delta": {
                "from_context_pack_id": context_pack_id,
                "to_context_pack_id": context_pack_id,
                "status": "current",
            },
            "mandatory_refresh": [],
        },
        "skills": {
            "unchanged": [{"id": "skill-a", "version": 1}],
            "updated": [],
            "missing": [],
            "stale_client_versions": [],
            "removed": [],
        },
        "handoffs": {
            "unchanged": [],
            "updated": [],
            "missing": [],
            "refresh_required": [],
        },
        "authoritative_state": {
            "checkpoint_version": 0,
            "context_pack_id": context_pack_id,
            "context_pack_fingerprint": "sha256:" + fingerprint_char * 64,
            "skills": {"skill-a": 1},
            "handoffs": {},
        },
        "warnings": [],
    }


def test_full_delivery_renders_context(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RECALL_HOME", str(tmp_path / "home"))
    _set_scope_env(monkeypatch, repository="repo-full")
    module = _load_hook()

    calls = []
    fake = _make_fake_urlopen(
        {
            "/v2/tasks": {"id": "task-full-1", "idempotent_replay": False},
            "/v2/context/deliver": _full_delivery(body_text="Remember to run tests."),
        },
        calls,
    )
    exit_code = _run_hook(
        module,
        monkeypatch,
        {"session_id": "s-full", "cwd": str(tmp_path), "source": "startup"},
        fake,
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "Remember to run tests." in context
    assert "recalled memory" in context

    assert any("/v2/tasks" in url for url, _ in calls)
    assert any("/v2/context/deliver" in url for url, _ in calls)

    # Codex's state file lives in the same .cc_cag_state directory as Claude
    # Code's but under a "codex_" prefixed filename -- see
    # recall_client._state_path().
    state_path = tmp_path / "home" / ".cc_cag_state" / "codex_repo-full.json"
    assert state_path.exists()
    state = json.loads(state_path.read_text())
    assert state["task_id"] == "task-full-1"
    assert state["known_context_pack_id"] == "context-1"
    assert state["known_context_pack_fingerprint"] == "sha256:" + "a" * 64
    # This client never opens a session/checkpoint.
    assert state["known_checkpoint_version"] is None

    # The delivery request itself must self-identify as codex (this adapter
    # passes agent_id=AGENT_ID into derive_scope(), not the module default).
    deliver_bodies = [body for url, body in calls if "/v2/context/deliver" in url]
    assert deliver_bodies[0]["agent_id"] == "codex"


def test_no_change_emits_nothing_and_skips_bootstrap(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RECALL_HOME", str(tmp_path / "home"))
    _set_scope_env(monkeypatch, repository="repo-nochange")

    state_dir = tmp_path / "home" / ".cc_cag_state"
    state_dir.mkdir(parents=True)
    (state_dir / "codex_repo-nochange.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "task_id": "task-cached",
                "client_id": "claude-code:repo-nochange",
                "known_checkpoint_version": None,
                "known_context_pack_id": "context-1",
                "known_context_pack_fingerprint": "sha256:" + "a" * 64,
                "known_skills": {"skill-a": 1},
                "known_handoffs": {},
                "updated_at": 0.0,
            }
        )
    )

    module = _load_hook()
    calls = []
    fake = _make_fake_urlopen(
        {"/v2/context/deliver": _no_change_delivery()},
        calls,
    )
    exit_code = _run_hook(
        module,
        monkeypatch,
        {"session_id": "s-nc", "cwd": str(tmp_path), "source": "resume"},
        fake,
    )
    assert exit_code == 0
    assert capsys.readouterr().out == ""
    # A cached task_id means bootstrap must never be attempted.
    assert not any("/v2/tasks" in url for url, _ in calls)
    assert any("/v2/context/deliver" in url for url, _ in calls)


@pytest.mark.parametrize("source", ["clear", "compact"])
def test_clear_and_compact_sources_suppress_injection(
    tmp_path, monkeypatch, capsys, source
):
    """Codex fires SessionStart for clear/compact too, but this hook must not
    inject there -- those events already carry the session's own context."""
    monkeypatch.setenv("RECALL_HOME", str(tmp_path / "home"))
    _set_scope_env(monkeypatch, repository="repo-suppressed")
    module = _load_hook()

    def _boom(*args, **kwargs):
        raise AssertionError("no network call should happen for clear/compact")

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps({"session_id": "s-sup", "cwd": str(tmp_path), "source": source})
        ),
    )
    assert module.main() == 0
    assert capsys.readouterr().out == ""


def test_task_bootstrap_failure_falls_back_to_v1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RECALL_HOME", str(tmp_path / "home"))
    _set_scope_env(monkeypatch, repository="repo-bootstrap-fail")
    module = _load_hook()

    calls = []
    fake = _make_fake_urlopen(
        {
            "/v2/tasks": ConnectionError("bootstrap unreachable"),
            "/search": {
                "results": [{"summary": "fallback memory", "category": "note"}]
            },
        },
        calls,
    )
    exit_code = _run_hook(
        module, monkeypatch, {"session_id": "s-bf", "cwd": str(tmp_path)}, fake
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "fallback memory" in out
    assert not any("/v2/context/deliver" in url for url, _ in calls)


def test_deliver_and_v1_both_failing_exits_zero_silently(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RECALL_HOME", str(tmp_path / "home"))
    _set_scope_env(monkeypatch, repository="repo-all-fail")
    module = _load_hook()

    calls = []
    fake = _make_fake_urlopen(
        {
            "/v2/tasks": ConnectionError("down"),
            "/search": ConnectionError("also down"),
        },
        calls,
    )
    exit_code = _run_hook(
        module, monkeypatch, {"session_id": "s-fail", "cwd": str(tmp_path)}, fake
    )
    assert exit_code == 0
    assert capsys.readouterr().out == ""


def test_client_state_round_trips_across_invocations(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RECALL_HOME", str(tmp_path / "home"))
    _set_scope_env(monkeypatch, repository="repo-roundtrip")

    calls1 = []
    module1 = _load_hook()
    fake1 = _make_fake_urlopen(
        {
            "/v2/tasks": {"id": "task-rt"},
            "/v2/context/deliver": _full_delivery(body_text="First run content."),
        },
        calls1,
    )
    exit_code1 = _run_hook(
        module1, monkeypatch, {"session_id": "s-rt-1", "cwd": str(tmp_path)}, fake1
    )
    assert exit_code1 == 0
    assert "First run content." in capsys.readouterr().out

    state_path = tmp_path / "home" / ".cc_cag_state" / "codex_repo-roundtrip.json"
    assert state_path.exists()
    saved = json.loads(state_path.read_text())
    assert saved["task_id"] == "task-rt"
    assert saved["known_context_pack_id"] == "context-1"

    calls2 = []
    module2 = _load_hook()
    fake2 = _make_fake_urlopen(
        {"/v2/context/deliver": _no_change_delivery()},
        calls2,
    )
    exit_code2 = _run_hook(
        module2, monkeypatch, {"session_id": "s-rt-2", "cwd": str(tmp_path)}, fake2
    )
    assert exit_code2 == 0
    assert capsys.readouterr().out == ""
    assert not any("/v2/tasks" in url for url, _ in calls2), "bootstrap not skipped"

    deliver_bodies = [body for url, body in calls2 if "/v2/context/deliver" in url]
    assert deliver_bodies, "deliver was never called on the second run"
    sent_state = deliver_bodies[0].get("client_state")
    assert sent_state is not None
    assert sent_state["known_context_pack_id"] == "context-1"
    assert sent_state["known_context_pack_fingerprint"] == "sha256:" + "a" * 64
    assert "known_checkpoint_version" not in sent_state


def test_malformed_stdin_exits_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RECALL_HOME", str(tmp_path / "home"))
    _set_scope_env(monkeypatch, repository="repo-malformed")
    module = _load_hook()

    import urllib.request

    fake = _make_fake_urlopen(
        {"/v2/tasks": ConnectionError("down"), "/search": ConnectionError("down")}, []
    )
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(sys, "stdin", io.StringIO("not-json"))
    assert module.main() == 0
    assert capsys.readouterr().out == ""


def test_empty_stdin_exits_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RECALL_HOME", str(tmp_path / "home"))
    _set_scope_env(monkeypatch, repository="repo-empty-stdin")
    module = _load_hook()

    import urllib.request

    fake = _make_fake_urlopen(
        {"/v2/tasks": ConnectionError("down"), "/search": ConnectionError("down")}, []
    )
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert module.main() == 0
    assert capsys.readouterr().out == ""


def test_old_shim_path_still_works(tmp_path, monkeypatch, capsys):
    """The pre-restructure path must keep working as a delegating shim.

    Anything still pointing Codex's SessionStart hook at
    `integrations/codex/recall_session.py` (the old checkout-relative
    location) must not break: that file is now a thin shim that imports and
    calls the packaged implementation's `main()`. This exercises it exactly
    the way the real hook runner would -- piped stdin, exit code, and a
    stdout-must-stay-clean check, since stdout is the hook's JSON protocol
    channel and any stray text there would corrupt what the host parses.
    """
    monkeypatch.setenv("RECALL_HOME", str(tmp_path / "home"))
    _set_scope_env(monkeypatch, repository="repo-old-shim")
    module = _load_hook(OLD_SHIM_HOOK_PATH, name="recall_codex_session_old_shim")

    import urllib.request

    fake = _make_fake_urlopen(
        {"/v2/tasks": ConnectionError("down"), "/search": ConnectionError("down")}, []
    )
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(sys, "stdin", io.StringIO("not-json"))
    assert module.main() == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "deprecated" in captured.err


def test_non_git_cwd_still_works(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RECALL_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("RECALL_REPOSITORY_ID", raising=False)
    monkeypatch.delenv("RECALL_USER_ID", raising=False)
    monkeypatch.delenv("RECALL_WORKSPACE_ID", raising=False)
    monkeypatch.delenv("RECALL_PROJECT_ID", raising=False)
    non_git_dir = tmp_path / "plain_dir"
    non_git_dir.mkdir()

    module = _load_hook()

    # Force every git invocation to fail, regardless of whether an ancestor
    # directory happens to be inside an unrelated git repository (git walks
    # up the tree looking for `.git`, so this is not guaranteed to be false
    # just because `non_git_dir` itself has no `.git`).
    def _no_git(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(module.recall_client.subprocess, "run", _no_git)

    scope = module.recall_client.derive_scope(str(non_git_dir))
    assert scope["repository_id"].startswith("path:")

    calls = []
    fake = _make_fake_urlopen(
        {
            "/v2/tasks": {"id": "task-ng"},
            "/v2/context/deliver": _full_delivery(body_text="Non-git content."),
        },
        calls,
    )
    exit_code = _run_hook(
        module, monkeypatch, {"session_id": "s-ng", "cwd": str(non_git_dir)}, fake
    )
    assert exit_code == 0
    assert "Non-git content." in capsys.readouterr().out


def test_codex_and_claude_state_files_do_not_collide(tmp_path, monkeypatch, capsys):
    """Codex and Claude Code must never read or write each other's on-disk
    CAG client state for the same repository."""
    monkeypatch.setenv("RECALL_HOME", str(tmp_path / "home"))
    _set_scope_env(monkeypatch, repository="repo-shared")

    codex_module = _load_hook()
    calls_codex = []
    fake_codex = _make_fake_urlopen(
        {
            "/v2/tasks": {"id": "task-codex"},
            "/v2/context/deliver": _full_delivery(
                context_pack_id="codex-pack",
                fingerprint_char="c",
                body_text="Codex content.",
            ),
        },
        calls_codex,
    )
    exit_code = _run_hook(
        codex_module,
        monkeypatch,
        {"session_id": "s-codex", "cwd": str(tmp_path), "source": "startup"},
        fake_codex,
    )
    assert exit_code == 0
    assert "Codex content." in capsys.readouterr().out

    claude_module = _load_hook(
        CLAUDE_HOOK_PATH, name="recall_claude_code_for_collision_check"
    )
    calls_claude = []
    fake_claude = _make_fake_urlopen(
        {
            "/v2/tasks": {"id": "task-claude"},
            "/v2/context/deliver": _full_delivery(
                context_pack_id="claude-pack",
                fingerprint_char="d",
                body_text="Claude content.",
            ),
        },
        calls_claude,
    )
    exit_code = _run_hook(
        claude_module,
        monkeypatch,
        {"session_id": "s-claude", "cwd": str(tmp_path)},
        fake_claude,
    )
    assert exit_code == 0
    assert "Claude content." in capsys.readouterr().out

    codex_state_path = tmp_path / "home" / ".cc_cag_state" / "codex_repo-shared.json"
    claude_state_path = (
        tmp_path / "home" / ".cc_cag_state" / "claude-code_repo-shared.json"
    )
    assert codex_state_path.exists()
    assert claude_state_path.exists()
    assert codex_state_path != claude_state_path

    codex_state = json.loads(codex_state_path.read_text())
    claude_state = json.loads(claude_state_path.read_text())
    assert codex_state["task_id"] == "task-codex"
    assert claude_state["task_id"] == "task-claude"
    assert codex_state["known_context_pack_id"] == "codex-pack"
    assert claude_state["known_context_pack_id"] == "claude-pack"


@pytest.mark.parametrize("first_agent", ["codex", "claude-code"])
def test_ambient_task_converges_regardless_of_bootstrap_order(
    tmp_path, monkeypatch, capsys, first_agent
):
    """Regression test for the shared-task attribution fix.

    recall_client.ensure_task() derives its idempotency key/title/objective/
    created_by from agent-NEUTRAL constants (_AMBIENT_TASK_IDENTITY), not
    from scope["agent_id"], specifically so that whichever adapter -- Codex
    or Claude Code -- bootstraps a repository's continuity task FIRST never
    causes the OTHER adapter's later bootstrap to fail. Before that fix,
    naively keying created_by off of the caller would make
    continuity.TaskService.create_task() raise `idempotency_conflict` for
    the second adapter (same idempotency key, different created_by), which
    ensure_task() swallows into a bare `None` return -- silently and
    permanently degrading that adapter to the v1 keyword-search fallback for
    this repository.

    Runs BOTH hooks against a REAL CAGDeliveryService/TaskService (no
    urlopen stubbing), first with `first_agent` bootstrapping the shared
    task and then the other adapter delivering against it, and asserts both
    succeed (real CAG delivery, not a v1 fallback) and land on the exact
    same task_id. Parametrized over both orders per the task requirement:
    "Codex-first bootstrap followed by a Claude delivery (and vice versa)".
    """
    import socket
    import tempfile
    from http.server import HTTPServer
    from threading import Thread

    from recall_mcp.api.rest import RecallHandler
    from recall_mcp.pipeline import MemoryPipeline
    from recall_mcp.runtime import Settings

    with tempfile.TemporaryDirectory() as tmpdir:
        settings = Settings(
            BASE_DIR=Path(tmpdir),
            DB_PATH=Path(tmpdir) / "memory.db",
            EMBEDDINGS_ENABLED=False,
        )
        pipeline = MemoryPipeline(settings, start_outbox_worker=False)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        RecallHandler.pipeline = pipeline
        RecallHandler.rate_limiter = None
        server = HTTPServer(("127.0.0.1", port), RecallHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            monkeypatch.setenv("RECALL_URL", f"http://127.0.0.1:{port}")
            monkeypatch.setenv("RECALL_HOME", str(tmp_path / "home"))
            repository = f"order-{first_agent}-repo"
            _set_scope_env(
                monkeypatch,
                user="order-user",
                workspace="order-ws",
                project="order-proj",
                repository=repository,
            )

            second_agent = "claude-code" if first_agent == "codex" else "codex"
            hooks = {
                "codex": (HOOK_PATH, "recall_codex_order_test"),
                "claude-code": (CLAUDE_HOOK_PATH, "recall_claude_order_test"),
            }
            state_files = {
                "codex": tmp_path
                / "home"
                / ".cc_cag_state"
                / f"codex_{repository}.json",
                "claude-code": (
                    tmp_path
                    / "home"
                    / ".cc_cag_state"
                    / f"claude-code_{repository}.json"
                ),
            }

            def _run(agent: str, session_id: str) -> None:
                path, name = hooks[agent]
                module = _load_hook(path, name=f"{name}_{session_id}")
                monkeypatch.setattr(
                    sys,
                    "stdin",
                    io.StringIO(
                        json.dumps(
                            {
                                "session_id": session_id,
                                "cwd": str(tmp_path),
                                "source": "startup",
                            }
                        )
                    ),
                )
                assert module.main() == 0

            _run(first_agent, f"{first_agent}-first")
            capsys.readouterr()  # drain stdout between the two hook runs

            _run(second_agent, f"{second_agent}-second")
            capsys.readouterr()

            first_state = json.loads(state_files[first_agent].read_text())
            second_state = json.loads(state_files[second_agent].read_text())

            # The whole point of this test: neither adapter's bootstrap ever
            # returns None here (which is what an idempotency_conflict would
            # cause), and both land on the exact same shared task.
            assert first_state["task_id"]
            assert second_state["task_id"]
            assert first_state["task_id"] == second_state["task_id"]

            # Per-agent attribution must still hold: each state file's
            # client_id is namespaced by its OWN agent, not the other one's.
            assert first_state["client_id"] == f"{first_agent}:{repository}"
            assert second_state["client_id"] == f"{second_agent}:{repository}"
        finally:
            server.shutdown()
            server.server_close()
            pipeline.close()


def test_real_service_end_to_end_delivery(tmp_path, monkeypatch, capsys):
    """Drive the hook against a REAL CAGDeliveryService over real HTTP.

    No urlopen stubbing here: this proves the exact JSON bodies
    recall_client.ensure_task/deliver_context build for Codex actually
    satisfy the server's own request validation (ContextPackRequest.validated(),
    ClientState.validated(), CAGDeliveryService._resolve_scope's mandatory
    task_id lookup), not just a test's assumption about the contract.
    """
    import socket
    import tempfile
    from http.server import HTTPServer
    from threading import Thread

    from recall_mcp.api.rest import RecallHandler
    from recall_mcp.pipeline import MemoryPipeline
    from recall_mcp.runtime import Settings

    with tempfile.TemporaryDirectory() as tmpdir:
        settings = Settings(
            BASE_DIR=Path(tmpdir),
            DB_PATH=Path(tmpdir) / "memory.db",
            EMBEDDINGS_ENABLED=False,
        )
        pipeline = MemoryPipeline(settings, start_outbox_worker=False)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        RecallHandler.pipeline = pipeline
        RecallHandler.rate_limiter = None
        server = HTTPServer(("127.0.0.1", port), RecallHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            monkeypatch.setenv("RECALL_URL", f"http://127.0.0.1:{port}")
            monkeypatch.setenv("RECALL_HOME", str(tmp_path / "home"))
            _set_scope_env(
                monkeypatch,
                user="e2e-user",
                workspace="e2e-ws",
                project="e2e-proj",
                repository="e2e-repo-codex",
            )

            module1 = _load_hook()
            monkeypatch.setattr(
                sys,
                "stdin",
                io.StringIO(
                    json.dumps(
                        {
                            "session_id": "e2e-1",
                            "cwd": str(tmp_path),
                            "source": "startup",
                        }
                    )
                ),
            )
            assert module1.main() == 0
            out1 = capsys.readouterr().out

            state_path = (
                tmp_path / "home" / ".cc_cag_state" / "codex_e2e-repo-codex.json"
            )
            assert state_path.exists()
            state1 = json.loads(state_path.read_text())
            assert state1["task_id"]
            assert state1["known_context_pack_id"]

            if out1:
                payload1 = json.loads(out1)
                assert payload1["hookSpecificOutput"]["hookEventName"] == "SessionStart"

            module2 = _load_hook()
            monkeypatch.setattr(
                sys,
                "stdin",
                io.StringIO(
                    json.dumps(
                        {
                            "session_id": "e2e-2",
                            "cwd": str(tmp_path),
                            "source": "resume",
                        }
                    )
                ),
            )
            assert module2.main() == 0
            out2 = capsys.readouterr().out

            # Second run reuses the same task and reports strictly less (or
            # equal) output -- the actual Phase 6 token-saving behavior, not
            # a re-send of the full pack.
            state2 = json.loads(state_path.read_text())
            assert state2["task_id"] == state1["task_id"]
            assert len(out2) <= len(out1)
        finally:
            server.shutdown()
            server.server_close()
            pipeline.close()
