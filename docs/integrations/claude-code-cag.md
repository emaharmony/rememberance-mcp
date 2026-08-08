# Claude Code CAG integration

The Claude Code `SessionStart` hook
(`recall_mcp.integrations.claude_code.inject_context`, packaged inside
`recall-mcp` -- `pip install recall-mcp` is enough, no source checkout
required) calls the Phase 6 CAG delivery endpoint, `POST /v2/context/deliver`,
so repeat sessions in the same repository receive only what changed instead
of the full context pack every time. Shared HTTP/git/state logic lives in
`recall_mcp.integrations.recall_client`, reused unmodified by the Codex
adapter (`recall_mcp.integrations.codex.recall_session`); `recall_mcp.
integrations.compat_env` stays a zero-dependency leaf that only resolves env
vars and `RECALL_HOME`. The old checkout-relative paths
(`integrations/claude-code/inject_context.py`, `integrations/recall_client.py`)
still work as thin backward-compatible shims that delegate to the packaged
modules, but are no longer the reference implementation.

## Why a task is required

`CAGDeliveryService._resolve_scope` (`src/recall_mcp/cag.py`) unconditionally
loads the request's `task_id` via `TaskService.get_task(...)` before it will
deliver anything -- there is no "scope-only, no task" mode. A delivery
request with no real, pre-existing task therefore fails with `not_found`
(`str(None)` becomes the literal string `"None"`, which never matches a row).

To make delivery possible, the hook bootstraps (or reuses) a task via
`POST /v2/tasks` before every delivery call, using an idempotency key derived
only from the stable scope identifiers plus a fixed, agent-NEUTRAL tag
(`recall_client._AMBIENT_TASK_IDENTITY`):

```
idempotency_key = "recall-ambient:" + sha256(f"{user_id}|{workspace_id}|{project_id}|{repository_id}|recall-ambient")[:32]
```

The key, `title`, `objective`, and `created_by` are identical regardless of
which adapter (Claude Code or Codex) sends the request -- this is
deliberate, not an oversight. `TaskService.create_task`'s idempotent-replay
check compares `{workspace_id, project_id, repository_id, title, objective,
created_by}` against the stored row for the same key, raising
`idempotency_conflict` on any mismatch; if any of those fields varied by
calling agent, whichever adapter bootstrapped a repository SECOND would
permanently fail and silently degrade to the v1 fallback. Keeping every
compared field agent-neutral means Claude Code and Codex always converge on
ONE shared continuity task per repository regardless of bootstrap order --
which is also required for cross-agent handoffs, since handoffs are
task-scoped (see [agent handoffs](../architecture/agent-handoffs.md)).
`created_by` is set to the synthetic, stable identity `"recall-ambient"`,
which `TaskService._ensure_agent` auto-registers in the `agents` table on
first use (no allowlist to satisfy). Per-agent attribution is preserved
where it actually matters instead: `agent_id` on every
`/v2/context/deliver` request (see below).

`branch` and `commit_sha` are also deliberately excluded from both the key
and from `title`/`objective`, so switching branches on the same repository
always replays the same task instead of conflicting, and bootstrap is
skipped entirely once a `task_id` is cached (see below).

## Client-state cache

After the first successful delivery, the hook persists the server's
`authoritative_state` to:

```
<RECALL_HOME>/.cc_cag_state/claude-code_<repository_id>.json
```

One file per (agent, repository) pair, so state (and thus token savings)
compounds across every session in that repo, not just within a single
session, and Claude Code's cache is never read or overwritten by Codex's
(`<RECALL_HOME>/.cc_cag_state/codex_<repository_id>.json`) even though both
adapters share the same underlying task. Shape:

```json
{
  "schema_version": 1,
  "task_id": "task_...",
  "client_id": "claude-code:<repository_id>",
  "known_checkpoint_version": null,
  "known_context_pack_id": "context_...",
  "known_context_pack_fingerprint": "sha256:...",
  "known_skills": {},
  "known_handoffs": {},
  "updated_at": 1710000000.0
}
```

`known_checkpoint_version` is always `null` and never sent back to the
server: this client does not open sessions or create checkpoints (see
"Not implemented" below), and `CAGRequest.validated()` raises if
`known_checkpoint_version` is set without a `session_id`. If
`known_context_pack_fingerprint` isn't a `sha256:`-prefixed digest for any
reason, it is dropped from the outgoing request rather than sent malformed
(`ClientState.validated()` in `src/recall_mcp/cag.py` rejects it outright).

Writes are atomic (temp file + `os.replace()`) and every read/write failure
is swallowed -- a corrupt or missing state file just means the next call
requests a full delivery, which is always safe.

## Capabilities advertised

The client's `client_state.capabilities` requests:

```
structured_json, context_delta, skill_delta, handoff_delta
```

filtered at call time against the server's actual `CAG_CAPABILITIES`
constant (`src/recall_mcp/cag.py`), so an older/newer server never receives
a capability it doesn't recognize. The top-level (`ContextPackRequest`-level)
`client_capabilities` field is validated against a *different* set
(`context.SUPPORTED_CAPABILITIES`), which only overlaps on `structured_json`
-- so only that one is sent there, to avoid a harmless-but-noisy
`unsupported_capability` warning on every delivery.

## Rendering by delivery mode

- `full` / `fallback_full` -- `context.full["inline_context"]` (already
  server-rendered markdown) under a `## Recall — recalled memory` heading.
- `delta` -- a compact "what changed" block built from `context.delta`'s
  changed subsections (task, constraints, decisions, work_state, session
  checkpoint advance, warnings, validation requests), plus a one-line count
  of any skill/handoff updates.
- `no_change` -- nothing is emitted.
- `refresh_required` -- the mandatory-refresh list (stale/removed
  skills/handoffs the client must discard) followed by `context.full` if the
  server included it.

## Fallback ladder

1. CAG delivery (`POST /v2/tasks` then `POST /v2/context/deliver`)
2. v1 keyword search (`GET /search?mode=keyword`, unchanged from before this
   change; extracted into `_v1_fallback()`)
3. No context at all

Every stage swallows its own exceptions and the hook always exits `0`. A
down, unreachable, or misbehaving Recall server never blocks or delays a
Claude Code session. Per-stage timeouts are budgeted inside `RECALL_TIMEOUT`
(bootstrap capped at `min(RECALL_TIMEOUT, 3s)`, delivery at
`min(RECALL_TIMEOUT, 5s)`) so the combined worst case stays comfortably under
Claude Code's hook timeout.

## Env overrides

| Variable | Purpose |
| --- | --- |
| `RECALL_URL` | Base URL of the Recall service (default `http://127.0.0.1:18790`) |
| `RECALL_TIMEOUT` | Base per-request timeout in seconds (default `6`); bootstrap/delivery budgets are derived from it |
| `RECALL_MAX_TOKENS` | Context token budget passed to `/v2/context/deliver` |
| `RECALL_INJECT_LIMIT` | v1 fallback result count (legacy, keyword search only) |
| `RECALL_USER_ID` | Overrides automatic user scope (default `local:<OS username>`) |
| `RECALL_WORKSPACE_ID` | Overrides automatic workspace scope (default: same as user) |
| `RECALL_PROJECT_ID` | Overrides automatic project scope (default: same as repository) |
| `RECALL_REPOSITORY_ID` | Overrides automatic repository scope (default: derived from `git remote get-url origin`, or the repo path if there is no remote) |
| `RECALL_HOME` | Overrides where `.cc_cag_state/` (and the existing `.cc_cursors/`, `.cc_outbox/`) live |

All of the above resolve through `compat_env.get_env()`, so the legacy
`REMEMBRANCE_*` names still work as a deprecated fallback.

A derived git remote URL has any embedded credential (`https://user:TOKEN@host/...`)
stripped before it is hashed into `repository_id` or sent as `remote_url` in
the task-bootstrap request -- see `recall_client._strip_credentials`.

## Not implemented client-side

Sessions, checkpoints, and skill/handoff client integration remain
unimplemented in this hook. It never calls `session_id`-related endpoints,
never sends `known_checkpoint_version`, and does not act on the
`skills`/`handoffs` sections of a delivery response beyond counting them in
the `delta` rendering. The Stop hook (`capture_transcript.py`) remains a
capture adapter, not the canonical cache. See
[Stop-hook diagnostics](../operations/stop-hook-diagnostics.md).
