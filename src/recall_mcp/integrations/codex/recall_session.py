#!/usr/bin/env python3
"""Codex SessionStart hook — inject recalled memory.

Primary path: the Phase 6 CAG delivery endpoint (`POST /v2/context/deliver`),
via the shared `recall_client` module also used by the Claude Code adapter.
It is version-aware, so repeat sessions in the same repository receive only
what changed (`delta`), nothing (`no_change`), or a full pack (`full`) the
first time -- instead of re-sending the same context on every SessionStart.

Delivery requires a real, pre-existing `task_id` (the server resolves scope
by loading the task row), so this hook first bootstraps/reuses a task via
`POST /v2/tasks` keyed by a stable per-repository idempotency key.

Falls back to the v1 `GET /search?mode=keyword` endpoint if CAG delivery is
unavailable for any reason (task bootstrap failed, delivery failed, server
down), and emits nothing at all if both fail. Sessions, checkpoints, and
skill/handoff client integration are out of scope for this hook -- only
context delivery is implemented. This mirrors
`claude_code/inject_context.py` closely; see that module for the reference
CAG-first flow.

Codex fires SessionStart on `startup`, `resume`, `clear`, and `compact`. We
only inject on `startup` and `resume` -- `clear`/`compact` already carry the
session's own context and re-injecting there is noise. This filter is
Codex-specific and is applied before any network calls.

The output shape (`hookSpecificOutput.hookEventName` / `.additionalContext`
on stdout) is identical to the Claude Code SessionStart contract -- that is
what Codex's hook runner expects here too, not a Codex-specific shape.

`recall_client.derive_scope(cwd, agent_id=AGENT_ID)` reports this adapter's
identity ("codex") everywhere per-agent attribution matters -- context
delivery requests, CAG delivery audit rows -- while `recall_client.
ensure_task()` deliberately keeps the `/v2/tasks` continuity row's
`created_by`/idempotency-key/title/objective agent-NEUTRAL (see
`recall_client._AMBIENT_TASK_IDENTITY`), so this adapter and the Claude Code
one always converge on the SAME continuity task for a given repository
instead of conflicting or forking one per agent. See `recall_client.
ensure_task()`'s docstring for why that sharing is required (task-scoped
handoffs) and safe (no `idempotency_conflict` regardless of bootstrap
order). The on-disk CAG client-state file, by contrast, IS kept fully
separate per agent -- see `recall_client._state_path()`.

Pure stdlib -- runs under any Python 3. Never blocks a session: any failure
(Recall down, bad JSON, timeout) exits 0 with no output.

Env:
  RECALL_URL           base URL of the Recall service (default 127.0.0.1:18790)
  RECALL_TIMEOUT       base per-request timeout in seconds (default 6); the
                        task-bootstrap call is capped at min(this, 2s), the
                        delivery call at min(this, 3s), and the v1 fallback
                        at min(this, 3s) -- so the worst-case combined hook
                        (bootstrap fails, then v1 also fails: 2s + 3s = 5s;
                        or bootstrap succeeds, deliver fails, v1 fails: 2s +
                        3s + 3s = 8s) stays comfortably under Codex's
                        `hooks.json` `timeoutSec` of 10 for this hook.
  RECALL_MAX_TOKENS    context token budget passed to /v2/context/deliver
  RECALL_INJECT_LIMIT  v1 fallback result count (legacy, keyword search only)
  RECALL_USER_ID / RECALL_WORKSPACE_ID / RECALL_PROJECT_ID / RECALL_REPOSITORY_ID
                        override automatic scope derivation --
                        see recall_client.derive_scope()
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

# Absolute package imports, not relative ones: this module is invoked both as
# `python -m recall_mcp.integrations.codex.recall_session` and as a bare
# script (`python .../recall_session.py`), which is how Codex's hook runner
# calls it. Direct-script execution loads this file as `__main__` with no
# package context, so `from . import recall_client` would raise "attempted
# relative import with no known parent package". Absolute imports work in
# both invocation modes as long as recall_mcp itself is installed (editable
# or wheel) in the interpreter that runs the hook -- which it always is,
# since the hook command invokes that same venv's python.exe.
from recall_mcp.integrations import recall_client
from recall_mcp.integrations.compat_env import get_env

RECALL_URL = get_env("URL", "http://127.0.0.1:18790").rstrip("/")
TIMEOUT = float(get_env("TIMEOUT", "6"))
BOOTSTRAP_TIMEOUT = min(TIMEOUT, 2.0) if TIMEOUT > 0 else 2.0
DELIVER_TIMEOUT = min(TIMEOUT, 3.0) if TIMEOUT > 0 else 3.0
V1_TIMEOUT = min(TIMEOUT, 3.0) if TIMEOUT > 0 else 3.0
LIMIT = int(get_env("INJECT_LIMIT", "8"))
MAX_TOKENS = get_env("MAX_TOKENS")

# Codex SessionStart source: startup|resume|clear|compact. clear/compact are
# deliberately excluded -- see the module docstring.
INJECT_SOURCES = {"startup", "resume"}

# Distinct from Claude Code's "claude-code": passed to
# recall_client.derive_scope()/new_client_state() and used to namespace this
# adapter's on-disk CAG client-state file (see recall_client._state_path()).
# Also passed as `scope["agent_id"]` on every CAG request this adapter
# makes. Distinct from the agent-neutral identity ensure_task() uses for the
# shared per-repository continuity task -- see
# recall_client._AMBIENT_TASK_IDENTITY.
AGENT_ID = "codex"


def _project_from_cwd(cwd: str) -> str:
    name = os.path.basename((cwd or "").rstrip("/\\"))
    return name or "codex"


def _format_v1(results: list, project: str) -> str:
    lines = []
    for m in results:
        text = (m.get("summary") or m.get("content") or "").strip()
        if not text:
            continue
        tag = m.get("category") or m.get("tier") or ""
        suffix = f" _({tag})_" if tag else ""
        lines.append(f"- {text}{suffix}")
    if not lines:
        return ""
    return f"## Recall — recalled memory for **{project}**\n\n" + "\n".join(lines)


def _v1_fallback(cwd: str) -> str:
    """Legacy v1 keyword search. Used only when CAG delivery is unavailable."""
    project = _project_from_cwd(cwd)
    query = urllib.parse.urlencode(
        {"q": project, "mode": "keyword", "limit": str(LIMIT)}
    )
    try:
        with urllib.request.urlopen(
            f"{RECALL_URL}/search?{query}", timeout=V1_TIMEOUT
        ) as resp:
            data = json.load(resp)
    except Exception:
        return ""
    return _format_v1(data.get("results", []) or [], project)


def _cag_context(cwd: str, session_id: str) -> str | None:
    """Try the Phase 6 CAG delivery path.

    Returns rendered markdown (possibly "" for a legitimately empty mode
    such as `no_change`), or None if the CAG path itself failed and the
    caller should fall back to v1.
    """
    try:
        scope = recall_client.derive_scope(cwd, agent_id=AGENT_ID)
    except Exception:
        return None

    try:
        state = recall_client.load_client_state(AGENT_ID, scope["repository_id"])
    except Exception:
        state = None

    task_id = state.get("task_id") if isinstance(state, dict) else None
    if not task_id:
        task_id = recall_client.ensure_task(RECALL_URL, BOOTSTRAP_TIMEOUT, scope)
        if not task_id:
            return None

    idempotency_key = f"codex:deliver:{session_id}" if session_id else None
    try:
        max_tokens = int(MAX_TOKENS) if MAX_TOKENS else None
    except (TypeError, ValueError):
        max_tokens = None

    delivery = recall_client.deliver_context(
        RECALL_URL,
        DELIVER_TIMEOUT,
        scope,
        task_id,
        state,
        idempotency_key=idempotency_key,
        max_tokens=max_tokens,
    )
    if delivery is None:
        return None

    try:
        authoritative = delivery.get("authoritative_state") or {}
        new_state = recall_client.new_client_state(
            task_id=task_id,
            agent_id=AGENT_ID,
            repository_id=scope["repository_id"],
            authoritative=authoritative,
        )
        recall_client.save_client_state(AGENT_ID, scope["repository_id"], new_state)
    except Exception:
        pass  # state persistence is best-effort; a cold cache next run is safe

    try:
        return recall_client.render_context(delivery)
    except Exception:
        return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        event = {}
    if not isinstance(event, dict):
        event = {}

    source = event.get("source", "startup")
    if source not in INJECT_SOURCES:
        return 0

    cwd = event.get("cwd") or os.getcwd()
    session_id = str(event.get("session_id") or "")

    markdown = _cag_context(cwd, session_id)
    if markdown is None:
        markdown = _v1_fallback(cwd)

    if not markdown:
        return 0

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": markdown,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
