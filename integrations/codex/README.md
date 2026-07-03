# Remembrance ↔ Codex integration

Give any Codex CLI session shared, persistent memory backed by Remembrance —
the same store Claude Code writes to. Three layers, use any combination:

1. **MCP server** — exposes `memory_search`, `memory_capture`,
   `memory_context_build`, `memory_graph_query`, `memory_dream` as tools Codex
   can call on demand.
2. **SessionStart hook** (`recall_session.py`) — automatically injects recalled
   memory for the current project at the start of every session
   (Codex fires this on `startup` and `resume`). No model decision required.
3. **Stop hook** (`capture_turn.py`) — automatically captures each turn's final
   assistant message into Remembrance. The gate decides what is worth keeping,
   so it is safe to forward everything.

There is also a `remembrance-flow` **skill** (under `~/.codex/skills/`) for
model-driven recall/capture over REST. The hooks make capture/recall
*automatic* instead of relying on the model to invoke the skill.

Both hook scripts are pure stdlib, run under any Python 3, and **never block a
session** — if Remembrance is down they exit 0 silently.

## Prerequisites

Remembrance running on `http://127.0.0.1:18790` (override with `REMEMBRANCE_URL`):

```
python -m remembrance_mcp.server.serve --host 127.0.0.1 --port 18790 --no-nats
```

Codex hooks must be enabled (they are `stable` and on by default in recent
builds; this makes it explicit):

```
codex features enable hooks
```

## 1. Register the MCP server

```powershell
codex mcp add remembrance -- `
  D:\_projects_\remembrance-mcp\.venv\Scripts\python.exe -m remembrance_mcp
```

This writes `[mcp_servers.remembrance]` into `~/.codex/config.toml`. The server
uses `REMEMBRANCE_HOME=~/.remembrance` by default — the same SQLite store as the
REST service and the Claude Code MCP server.

## 2. Add the hooks

Copy `hooks.json` to `~/.codex/hooks.json` (user scope = all projects), or merge
its `hooks` table into the `[hooks]` section of `~/.codex/config.toml`. Paths
must be absolute. The `project_id`/category is derived automatically from each
session's working directory.

Then **trust** the hooks — Codex refuses to run an untrusted command hook:

```
codex            # start a session
/hooks           # review and trust the two remembrance hooks
```

For non-interactive/automation use, `codex exec --dangerously-bypass-hook-trust`
skips the trust requirement for one invocation.

## Differences from the Claude Code integration

- **Capture granularity.** Claude's Stop hook reads the transcript file and
  forwards *both* new user and assistant text (cursor-tracked). Codex's Stop
  payload exposes only `last_assistant_message`, so this captures the assistant's
  final message per turn — one memory per Stop, no cursor needed. User-prompt
  text is not captured on the Codex side.
- **Events.** Codex supports `SessionStart` (startup/resume/clear/compact),
  `Stop`, `UserPromptSubmit`, `PreToolUse`, and `PostToolUse`. `PreToolUse`/
  `PostToolUse` match shell commands only.
- **Trust.** Codex requires explicit per-definition hook trust via `/hooks`;
  Claude Code does not.
