# Remembrance ↔ Claude Code integration

Give any Claude Code session shared, persistent memory backed by Remembrance.
Three layers, use any combination:

1. **MCP server** — exposes `memory_search`, `memory_capture`, `memory_context_build`,
   `memory_graph_query`, `memory_dream` as tools Claude can call on demand.
2. **SessionStart hook** (`inject_context.py`) — automatically injects recalled
   memory for the current project at the start of every session. No model
   decision required.
3. **Stop hook** (`capture_transcript.py`) — automatically captures new
   conversation into Remembrance at the end of each turn. The Remembrance gate
   decides what is worth keeping, so it is safe to forward everything.

Both hook scripts are pure stdlib, run under any Python 3, and **never block a
session** — if Remembrance is down they exit silently.

## Prerequisites

Remembrance running on `http://127.0.0.1:18790` (override with `REMEMBRANCE_URL`):

```
python -m remembrance_mcp.server.serve --host 127.0.0.1 --port 18790 --no-nats
# or, from Prism:  prism remembrance serve
```

## 1. Register the MCP server

```bash
claude mcp add remembrance --scope user -- \
  "D:/_projects_/remembrance-mcp/.venv/Scripts/python.exe" -m remembrance_mcp
```

On this Windows setup, the user-scoped Claude entry uses the included wrapper so
the MCP subprocess gets the correct working directory and Remembrance
environment:

```powershell
claude mcp add remembrance --scope user -- `
  D:\_projects_\remembrance-mcp\integrations\claude-code\remembrance_mcp_stdio.cmd
```

## 2. Add the hooks

Merge `settings.snippet.json` into `~/.claude/settings.json` (user scope = all
projects) or a project `.claude/settings.json`. Paths must be absolute. The
`project_id` is derived automatically from each session's working directory.

## Notes

- The Stop hook tracks a per-session cursor under `~/.remembrance/.cc_cursors/`
  so it only captures *new* messages, never duplicates the whole transcript.
- Only `text` from user/assistant messages is captured — tool calls, tool
  results, and thinking are ignored.
- To scope memory per project, install the hooks in that project's
  `.claude/settings.json` instead of the user file.
