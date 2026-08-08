# Recall and Codex

Give Codex sessions durable context backed by the same Recall store used by other agents.

The integration has three independent layers:

1. The Recall MCP server exposes memory tools on demand.
2. recall_session.py injects project context at SessionStart for startup and resume.
3. capture_turn.py sends the final assistant message at Stop.

The hooks are best-effort. Failures do not block the session, and compatibility warnings go to stderr.

## Prerequisites

~~~bash
recall-service --host 127.0.0.1 --port 18790 --no-nats
codex features enable hooks
~~~

## Register the MCP server

~~~powershell
codex mcp add recall -- recall-mcp
~~~

This creates a recall MCP server entry. Existing entries that invoke remembrance-mcp remain supported during migration.

## Install hooks

The hook scripts (`recall_session.py`, `capture_turn.py`) and their shared
helpers now ship inside the installed `recall_mcp` package at
`src/recall_mcp/integrations/codex/` and
`src/recall_mcp/integrations/compat_env.py` -- `pip install recall-mcp` is
enough to get them; there is nothing left to copy alongside the scripts.

Edit the repository paths in `hooks.json` (shipped at
`src/recall_mcp/integrations/codex/hooks.json`), then copy it to
~/.codex/hooks.json or merge its hooks table into your Codex configuration.
Review and trust the hook definitions with /hooks.

`integrations/codex/recall_session.py` and `capture_turn.py` still exist at
their old checkout-relative paths as thin backward-compat shims that
delegate to the packaged implementation, so existing hook registrations
keep working -- but new installs should point at the packaged module paths
above.

## Environment

| Variable | Default |
| --- | --- |
| RECALL_URL | http://127.0.0.1:18790 |
| RECALL_TIMEOUT | 30 seconds for capture, 6 seconds for injection |
| RECALL_MAX_TOKENS | unset (server default) |
| RECALL_INJECT_LIMIT | 8 |
| RECALL_HOME | safe home-resolution order |
| RECALL_USER_ID | unset |
| RECALL_WORKSPACE_ID | unset |
| RECALL_PROJECT_ID | unset |
| RECALL_REPOSITORY_ID | unset |

Matching REMEMBRANCE_* names remain lower-priority fallbacks during migration.

RECALL_USER_ID, RECALL_WORKSPACE_ID, RECALL_PROJECT_ID, and RECALL_REPOSITORY_ID
are all optional and pin the Recall scope identity used by this client; each
has a matching REMEMBRANCE_* legacy fallback. These are shared with the
Claude Code adapter -- both clients derive the same scope for the same
repository, since scope identifies the repository, not the tool talking to it.

## Session injection behavior

`recall_session.py` now prefers CAG (context-aware generation) delivery via
`POST /v2/context/deliver`, using the same shared `recall_client` module as
the Claude Code adapter, and falls back to the v1 keyword `/search` route if
the v2 endpoint is unavailable (task bootstrap failed, delivery failed, or
the server is down). Both paths failing emits nothing; a down or absent
Recall server never delays or blocks a Codex session.

Codex fires `SessionStart` for `startup`, `resume`, `clear`, and `compact`;
this hook only injects for `startup` and `resume` -- `clear`/`compact`
already carry the session's own context, so re-injecting there would just be
noise.

Two things are intentionally namespaced separately from Claude Code:

- **On-disk CAG client state** (`known_context_pack_id`, fingerprints, etc.)
  is stored under `<RECALL_HOME>/.cc_cag_state/codex_<repository_id>.json`,
  distinct from Claude Code's
  `<RECALL_HOME>/.cc_cag_state/claude-code_<repository_id>.json` for the same
  repository, so the two clients never read or overwrite each other's cached
  delivery state.
- **`agent_id`** sent on every context-delivery request is `"codex"`, not
  `"claude-code"`.

One thing is intentionally *shared* with Claude Code: the `/v2/tasks`
continuity task backing a repository. Both adapters derive the same
idempotency key, title, objective, and `created_by` (an agent-neutral
`"recall-ambient"` identity, not either agent's own id -- see
`recall_client._AMBIENT_TASK_IDENTITY`), so they always converge on one
continuity task per repository regardless of which adapter bootstraps it
first, with no `idempotency_conflict`. This is required, not just
convenient: handoffs are task-scoped (see
`docs/architecture/agent-handoffs.md`), so per-agent tasks would make a
Claude Code <-> Codex handoff impossible. Per-agent attribution is preserved
where it actually matters -- the `agent_id` on every context-delivery
request, above.

## Capture behavior

Codex Stop events expose last_assistant_message rather than the entire transcript, so one assistant response is considered per Stop event. Claude Code uses cursor-tracked transcript lines and can capture both user and assistant text.