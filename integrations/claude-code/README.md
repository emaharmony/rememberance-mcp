# Recall and Claude Code

Give Claude Code sessions shared, persistent context backed by Recall.

The integration has three independent layers:

1. The Recall MCP server exposes memory tools on demand.
2. inject_context.py searches at SessionStart and adds project context.
3. capture_transcript.py forwards new user and assistant text at Stop.

The hooks are best-effort and write protocol output only when they have valid context to inject. Compatibility warnings and diagnostics go to stderr.

## Prerequisites

Start Recall on the integration port:

~~~bash
recall-service --host 127.0.0.1 --port 18790 --no-nats
~~~

Override the hook URL with RECALL_URL.

## Register the MCP server

~~~bash
claude mcp add recall --scope user -- recall-mcp
~~~

For a source checkout on Windows, use the canonical wrapper:

~~~powershell
claude mcp add recall --scope user -- D:\path\to\recall\integrations\claude-code\recall_mcp_stdio.cmd
~~~

The deprecated remembrance_mcp_stdio.cmd filename delegates to the canonical wrapper.

## Install hooks

Edit the paths in settings.snippet.json, then merge its hooks table into either:

- ~/.claude/settings.json for all projects
- .claude/settings.json for one project

Keep integrations/compat_env.py with the hook scripts. It centralizes RECALL_URL, RECALL_TIMEOUT, RECALL_INJECT_LIMIT, and safe home-directory fallbacks.

The Stop hook stores cursors and temporary outbox files under the resolved Recall home. If only the legacy home exists, it keeps using that directory in place.

## Environment

| Variable | Default |
| --- | --- |
| RECALL_URL | http://127.0.0.1:18790 |
| RECALL_TIMEOUT | 30 seconds for capture, 6 seconds for injection |
| RECALL_INJECT_LIMIT | 8 |
| RECALL_HOME | safe home-resolution order |

Matching REMEMBRANCE_* names remain lower-priority fallbacks during migration.