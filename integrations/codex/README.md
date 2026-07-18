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

Edit the repository paths in hooks.json, then copy it to ~/.codex/hooks.json or merge its hooks table into your Codex configuration. Review and trust the hook definitions with /hooks.

Keep integrations/compat_env.py with the hook scripts. It centralizes RECALL_URL, RECALL_TIMEOUT, RECALL_INJECT_LIMIT, and home-directory compatibility.

## Environment

| Variable | Default |
| --- | --- |
| RECALL_URL | http://127.0.0.1:18790 |
| RECALL_TIMEOUT | 30 seconds for capture, 6 seconds for injection |
| RECALL_INJECT_LIMIT | 8 |
| RECALL_HOME | safe home-resolution order |

Matching REMEMBRANCE_* names remain lower-priority fallbacks during migration.

## Capture behavior

Codex Stop events expose last_assistant_message rather than the entire transcript, so one assistant response is considered per Stop event. Claude Code uses cursor-tracked transcript lines and can capture both user and assistant text.