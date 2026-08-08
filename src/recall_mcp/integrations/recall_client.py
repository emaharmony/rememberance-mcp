"""Shared HTTP, git, and client-state logic for Recall standalone integrations.

`compat_env.py` stays a zero-dependency leaf (env var and home-directory
resolution only). Everything that talks to the network, shells out to git, or
persists on-disk client state for the Phase 6 CAG delivery cache lives here
instead, so other adapters (e.g. a future Codex client) can adopt the same
logic without duplicating it.

Pure stdlib: no `requests`, and no import of anything from `recall_mcp`
itself beyond this integrations package (no `mcp`, `torch`, `transformers`,
etc.). This module ships inside the `recall_mcp` package now, so it does
require `recall_mcp` to be installed/importable -- but it must stay free of
recall_mcp's heavier optional dependencies, since it runs inside hooks that
fire on every session start/stop and must stay fast and dependency-light.
Every public function is best-effort -- git, filesystem, and HTTP failures
are swallowed and reported via `None`/empty return values rather than
exceptions, because these helpers run inside hooks that must never block or
fail a session.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from recall_mcp.integrations.compat_env import get_env, resolve_home

# Capabilities this client would like to use. Validated against the server's
# advertised CAG_CAPABILITIES (see src/recall_mcp/cag.py) before being sent,
# so an older/newer server never receives a capability it does not recognize.
_REQUESTED_CAPABILITIES = (
    "structured_json",
    "context_delta",
    "skill_delta",
    "handoff_delta",
)

# Mirrors CAG_CAPABILITIES in src/recall_mcp/cag.py as of Phase 6. Used only
# to filter _REQUESTED_CAPABILITIES; if the server later drops/renames one of
# these, the filter just becomes a no-op for that entry rather than raising.
_KNOWN_SERVER_CAPABILITIES = {
    "structured_json",
    "markdown",
    "context_delta",
    "skill_delta",
    "skill_reference",
    "reference_expansion",
    "handoff_delta",
}

# Client-state fields that are safe to echo back to the server. Deliberately
# excludes known_checkpoint_version: this client never creates sessions or
# checkpoints, and CAGRequest.validated() raises if known_checkpoint_version
# is set without a session_id.
_ECHOED_STATE_FIELDS = (
    "client_id",
    "client_type",
    "known_context_pack_id",
    "known_context_pack_fingerprint",
    "known_skills",
    "known_handoffs",
)

_STATE_SCHEMA_VERSION = 1

# Agent-neutral identity for the auto-bootstrapped, per-repository
# continuity task created by ensure_task() below. Used as the idempotency
# key's namespace tag AND as the task's `created_by`. Every standalone
# adapter (Claude Code, Codex, and any future one) must derive the exact
# same idempotency key/title/objective/created_by for the same
# (user, workspace, project, repository) so they always converge on ONE
# shared task -- handoffs are task-scoped (see handoff.py and
# docs/architecture/agent-handoffs.md), so per-agent tasks would make a
# Claude Code <-> Codex handoff impossible. See ensure_task()'s docstring
# for what breaks if this is naively made agent-specific instead.
_AMBIENT_TASK_IDENTITY = "recall-ambient"


def _run_git(cwd: str, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _sha256_prefix(value: str, length: int = 24) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _sanitize_filename(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in value)
    return safe or "unknown"


def _strip_credentials(remote_url: str) -> str:
    """Drop embedded userinfo (`user:token@`) from an http(s) remote URL.

    Git remotes are sometimes configured as `https://user:TOKEN@host/...`
    (e.g. CI checkouts, PAT-based auth). That credential must never be
    hashed-and-forgotten only in appearance -- it is also sent verbatim as
    `remote_url` to `POST /v2/tasks` and persisted server-side, so leaving it
    in would exfiltrate the token to the Recall server/database. SSH-style
    `git@host:org/repo.git` remotes are left untouched: the `git@` there is
    a fixed transport user, not a secret.
    """
    if not remote_url.lower().startswith(("http://", "https://")):
        return remote_url
    try:
        parsed = urllib.parse.urlsplit(remote_url)
        if "@" not in parsed.netloc:
            return remote_url
        netloc = parsed.netloc.rsplit("@", 1)[1]
        return urllib.parse.urlunsplit(parsed._replace(netloc=netloc))
    except Exception:
        # If it doesn't parse cleanly, do not risk forwarding a credential.
        return ""


def derive_scope(cwd: str, *, agent_id: str = "claude-code") -> dict[str, Any]:
    """Best-effort derivation of Recall scope identifiers for `cwd`.

    `agent_id` identifies the CALLING adapter (e.g. "claude-code", "codex")
    and is echoed verbatim into the returned scope's `agent_id` field, which
    callers then pass through unchanged to `deliver_context()` -- that is
    the per-agent attribution that actually matters (recorded on every
    `/v2/context/deliver` call). It defaults to "claude-code" to preserve
    that adapter's existing behavior; every other adapter must pass its own
    identity explicitly rather than mutating the returned dict afterward.
    Note this is unrelated to `ensure_task()`'s task attribution, which is
    deliberately agent-NEUTRAL -- see _AMBIENT_TASK_IDENTITY above.

    Never raises. Falls back to path-derived identifiers when `cwd` is not
    inside a git repository, git is unavailable, or any git call fails.
    """
    cwd = cwd or os.getcwd()
    user_id = get_env("USER_ID") or ("local:" + getpass.getuser())

    toplevel = _run_git(cwd, "rev-parse", "--show-toplevel")
    raw_remote_url = _run_git(cwd, "remote", "get-url", "origin")
    remote_url = _strip_credentials(raw_remote_url) if raw_remote_url else None
    branch = _run_git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    commit_sha = _run_git(cwd, "rev-parse", "HEAD")
    default_branch_ref = _run_git(
        cwd, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"
    )
    default_branch = None
    if default_branch_ref and "/" in default_branch_ref:
        default_branch = default_branch_ref.split("/", 1)[1]

    try:
        canonical_path = os.path.realpath(toplevel or cwd)
    except Exception:
        canonical_path = toplevel or cwd

    repository_id = get_env("REPOSITORY_ID")
    if not repository_id:
        if remote_url:
            normalized = remote_url.strip().rstrip("/")
            if normalized.lower().endswith(".git"):
                normalized = normalized[: -len(".git")]
            repository_id = "git:" + _sha256_prefix(normalized.lower())
        else:
            repository_id = "path:" + _sha256_prefix(canonical_path)

    project_id = get_env("PROJECT_ID") or repository_id
    workspace_id = get_env("WORKSPACE_ID") or user_id
    repository_name = os.path.basename(canonical_path.rstrip("/\\")) or repository_id

    return {
        "user_id": user_id,
        "workspace_id": workspace_id,
        "project_id": project_id,
        "repository_id": repository_id,
        "repository_name": repository_name,
        "agent_id": agent_id,
        "branch": branch,
        "commit_sha": commit_sha,
        "canonical_path": canonical_path,
        "remote_url": remote_url,
        "default_branch": default_branch or branch,
    }


def _state_dir() -> Path:
    return resolve_home() / ".cc_cag_state"


def _state_path(agent_id: str, repository_id: str) -> Path:
    """Build the on-disk state path, namespaced by BOTH agent and repository.

    Each adapter owns its own state file for a given repository -- Claude
    Code and Codex must never read or write each other's cached client
    state (see test_codex_and_claude_state_files_do_not_collide /
    test_cag_state_files_do_not_collide). Namespacing lives here, in the one
    module both adapters share, rather than in each caller pre-mangling the
    `repository_id` it passes in.
    """
    return _state_dir() / (
        f"{_sanitize_filename(agent_id)}_{_sanitize_filename(repository_id)}.json"
    )


def load_client_state(agent_id: str, repository_id: str) -> dict[str, Any] | None:
    """Load the previously persisted CAG client state for this agent+repository.

    Returns None on any failure (missing file, corrupt JSON, wrong shape) --
    callers should treat that identically to "no prior state".
    """
    try:
        path = _state_path(agent_id, repository_id)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def save_client_state(agent_id: str, repository_id: str, state: dict[str, Any]) -> None:
    """Persist CAG client state for this agent+repository, atomically.

    Never raises -- a failed write just means the next session starts cold
    (falls back to a full delivery), which is safe.
    """
    try:
        directory = _state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = _state_path(agent_id, repository_id)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".cc_cag_state-", suffix=".tmp", dir=str(directory)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle)
            os.replace(tmp_path, str(path))
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
    except Exception:
        return None


def _post_json(url: str, body: dict[str, Any], timeout: float) -> dict[str, Any] | None:
    try:
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def ensure_task(base_url: str, timeout: float, scope: dict[str, Any]) -> str | None:
    """Create (or idempotently replay) the task backing this repository.

    The idempotency key, title, objective, and `created_by` are all
    deliberately agent-NEUTRAL (see _AMBIENT_TASK_IDENTITY) -- none of them
    are derived from `scope["agent_id"]`. This is required, not cosmetic:
    continuity.TaskService.create_task() replays an existing row only when
    the idempotency key matches AND {workspace_id, project_id,
    repository_id, title, objective, created_by} all match too, raising
    `idempotency_conflict` otherwise. If any of those fields varied by
    caller, then whichever adapter (Claude Code or Codex) bootstraps a
    repository SECOND would send a different value than the first adapter
    already stored, permanently fail with `idempotency_conflict` on every
    subsequent call, and silently degrade to the v1 keyword-search fallback
    forever -- since `ensure_task()` would return None and this never
    self-heals. Keeping every compared field constant across all callers
    means the task is correctly shared once per repository regardless of
    which adapter (or order of adapters) bootstraps it first; this is also
    the correct behavior on its own terms, since handoffs are task-scoped
    (see handoff.py / docs/architecture/agent-handoffs.md) and per-agent
    tasks would make a Claude Code <-> Codex handoff impossible.

    `created_by` is a synthetic, stable identity ("recall-ambient"), not any
    real agent's id. continuity.TaskService._ensure_agent() upserts a row in
    the `agents` table for whatever string is passed here (INSERT if
    missing, else bump `last_seen_at`) -- there is no allowlist to satisfy,
    so this is a safe, self-registering placeholder rather than a
    masquerade as a real agent. Per-agent attribution is unaffected by this
    choice: it is `scope["agent_id"]` (see derive_scope()) that is sent on
    every `/v2/context/deliver` call and recorded there, which is where
    attribution actually matters for this ambient-context use case.

    The idempotency key still deliberately excludes `branch` and
    `commit_sha` -- title and objective are also branch-independent, so
    switching branches in the same repository always replays the same task
    instead of conflicting.

    Returns the task id, or None if the bootstrap request failed for any
    reason (server down, validation error, timeout).
    """
    repository_name = scope.get("repository_name") or scope["repository_id"]
    key_source = "|".join(
        [
            str(scope["user_id"]),
            str(scope["workspace_id"]),
            str(scope["project_id"]),
            str(scope["repository_id"]),
            _AMBIENT_TASK_IDENTITY,
        ]
    )
    idempotency_key = (
        _AMBIENT_TASK_IDENTITY
        + ":"
        + hashlib.sha256(key_source.encode("utf-8")).hexdigest()[:32]
    )
    body = {
        "user_id": scope["user_id"],
        "workspace_id": scope["workspace_id"],
        "project_id": scope["project_id"],
        "repository_id": scope["repository_id"],
        "title": f"Recall context: {repository_name}",
        "objective": f"Maintain Recall context continuity for {repository_name}.",
        "created_by": _AMBIENT_TASK_IDENTITY,
        "agent_system_type": "other",
        "idempotency_key": idempotency_key,
        "repository_name": repository_name,
        "canonical_path": scope.get("canonical_path"),
        "remote_url": scope.get("remote_url"),
        "default_branch": scope.get("default_branch"),
    }
    response = _post_json(base_url.rstrip("/") + "/v2/tasks", body, timeout)
    if not response:
        return None
    task_id = response.get("id")
    return str(task_id) if task_id else None


def deliver_context(
    base_url: str,
    timeout: float,
    scope: dict[str, Any],
    task_id: str,
    client_state: dict[str, Any] | None,
    *,
    idempotency_key: str | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any] | None:
    """Call `POST /v2/context/deliver` and return the raw response, or None.

    `client_state` is the locally persisted state dict (see
    load_client_state/save_client_state), not a recall_mcp.cag.ClientState --
    this module never imports recall_mcp. Only the fields the server accepts
    are echoed back; known_checkpoint_version is always omitted because this
    client never opens a session.
    """
    capabilities = [
        capability
        for capability in _REQUESTED_CAPABILITIES
        if capability in _KNOWN_SERVER_CAPABILITIES
    ]
    # `client_capabilities` (top-level, ContextPackRequest-level) and
    # `client_state.capabilities` (CAG-level) are validated against two
    # different sets -- context.SUPPORTED_CAPABILITIES vs cag.CAG_CAPABILITIES.
    # Only "structured_json" is common to both; sending the CAG-only entries
    # (context_delta, skill_delta, handoff_delta) at the top level would just
    # earn a harmless-but-noisy "unsupported_capability" warning.
    top_level_capabilities = [c for c in capabilities if c == "structured_json"]
    body: dict[str, Any] = {
        "user_id": scope["user_id"],
        "workspace_id": scope["workspace_id"],
        "project_id": scope["project_id"],
        "repository_id": scope["repository_id"],
        "task_id": task_id,
        "agent_id": scope.get("agent_id", "claude-code"),
        "client_capabilities": top_level_capabilities,
    }
    if scope.get("branch"):
        body["branch"] = scope["branch"]
    if scope.get("commit_sha"):
        body["commit_sha"] = scope["commit_sha"]
    if max_tokens:
        body["max_tokens"] = max_tokens
    if idempotency_key:
        body["idempotency_key"] = idempotency_key

    if client_state:
        state_body = {
            key: client_state[key]
            for key in _ECHOED_STATE_FIELDS
            if client_state.get(key) not in (None, {}, ())
        }
        fingerprint = state_body.get("known_context_pack_fingerprint")
        if fingerprint is not None and not str(fingerprint).startswith("sha256:"):
            # ClientState.validated() rejects a malformed fingerprint outright;
            # dropping it here just means the server treats state as unverified
            # for that one field instead of the whole delivery erroring out.
            state_body.pop("known_context_pack_fingerprint", None)
        if state_body:
            state_body["capabilities"] = capabilities
            body["client_state"] = state_body

    return _post_json(base_url.rstrip("/") + "/v2/context/deliver", body, timeout)


def new_client_state(
    *, task_id: str, agent_id: str, repository_id: str, authoritative: dict[str, Any]
) -> dict[str, Any]:
    """Build the on-disk state dict to persist after a successful delivery.

    Unlike the shared task (see ensure_task()), `client_id` here IS
    agent-specific -- it identifies which client cached this state, not
    which task it backs, so Claude Code and Codex report distinguishable
    client_ids for the same repository even while sharing one `task_id`.
    """
    fingerprint = authoritative.get("context_pack_fingerprint")
    return {
        "schema_version": _STATE_SCHEMA_VERSION,
        "task_id": task_id,
        "client_id": f"{agent_id}:{repository_id}",
        "known_checkpoint_version": None,
        "known_context_pack_id": authoritative.get("context_pack_id"),
        "known_context_pack_fingerprint": fingerprint
        if isinstance(fingerprint, str) and fingerprint.startswith("sha256:")
        else None,
        "known_skills": authoritative.get("skills") or {},
        "known_handoffs": authoritative.get("handoffs") or {},
        "updated_at": time.time(),
    }


_HEADING = "## Recall — recalled memory"


def _short(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("text", "summary", "title", "question", "decision"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return json.dumps(value, sort_keys=True)[:200]
    return str(value)


def _render_list_delta(label: str, delta: dict[str, Any] | None) -> list[str]:
    delta = delta or {}
    lines = []
    for item in delta.get("added") or []:
        lines.append(f"- + [{label}] {_short(item)}")
    for item in delta.get("removed") or []:
        lines.append(f"- - [{label}] {_short(item)}")
    return lines


_LIST_DELTA_SECTIONS = {
    "constraints": ("critical", "normal"),
    "decisions": ("approved", "proposed"),
    "work_state": (
        "completed",
        "remaining",
        "blockers",
        "open_questions",
        "important_files",
        "known_failures",
    ),
}


def _render_mandatory_refresh(items: list[dict[str, Any]]) -> str:
    lines = ["**Stale or removed items -- discard any cached copies:**"]
    for item in items:
        identifier = item.get("id", "unknown")
        reason = item.get("reason", "")
        suffix = f" ({reason})" if reason else ""
        lines.append(f"- `{identifier}`{suffix}")
    return "\n".join(lines)


def _render_delta(
    delta: dict[str, Any], skills: dict[str, Any], handoffs: dict[str, Any]
) -> str:
    lines: list[str] = []
    task = delta.get("task") or {}
    if task.get("changed"):
        current = task.get("current") or {}
        title = current.get("title") or current.get("id") or "(unspecified)"
        lines.append(f"- Active task changed: {title}")
    for section, subsections in _LIST_DELTA_SECTIONS.items():
        section_delta = delta.get(section) or {}
        for sub in subsections:
            lines.extend(_render_list_delta(f"{section}.{sub}", section_delta.get(sub)))
    session_delta = (delta.get("session") or {}).get("delta")
    if session_delta and not session_delta.get("no_change"):
        lines.append(
            "- Session checkpoint advanced: v{0} -> v{1}".format(
                session_delta.get("from_version"), session_delta.get("to_version")
            )
        )
    lines.extend(_render_list_delta("warnings", delta.get("warnings")))
    lines.extend(
        _render_list_delta("validation_requests", delta.get("validation_requests"))
    )
    skill_updates = len(skills.get("updated") or []) + len(skills.get("missing") or [])
    if skill_updates:
        lines.append(f"- Skills updated/added: {skill_updates}")
    handoff_updates = len(handoffs.get("updated") or []) + len(
        handoffs.get("missing") or []
    )
    if handoff_updates:
        lines.append(f"- Handoffs updated/added: {handoff_updates}")
    return "\n".join(lines)


def render_context(delivery: dict[str, Any]) -> str:
    """Render a `/v2/context/deliver` response into hook `additionalContext`.

    Returns "" when there is nothing worth injecting (e.g. `no_change`, or a
    mode whose payload turned out empty).
    """
    if not isinstance(delivery, dict):
        return ""
    mode = delivery.get("delivery_mode")
    context = delivery.get("context") or {}

    if mode in ("full", "fallback_full"):
        full = context.get("full") or {}
        body = str(full.get("inline_context") or "").strip()
        return f"{_HEADING}\n\n{body}" if body else ""

    if mode == "no_change":
        return ""

    if mode == "refresh_required":
        parts = []
        mandatory = context.get("mandatory_refresh") or []
        if mandatory:
            parts.append(_render_mandatory_refresh(mandatory))
        full = context.get("full") or {}
        body = str(full.get("inline_context") or "").strip()
        if body:
            parts.append(body)
        return (
            f"{_HEADING} (refresh required)\n\n" + "\n\n".join(parts) if parts else ""
        )

    if mode == "delta":
        delta = context.get("delta") or {}
        rendered = _render_delta(
            delta, delivery.get("skills") or {}, delivery.get("handoffs") or {}
        )
        return f"{_HEADING} (updates)\n\n{rendered}" if rendered else ""

    return ""
