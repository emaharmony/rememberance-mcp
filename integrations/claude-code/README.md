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

## One-command setup (recommended)

`pip install recall-mcp` and then one command registers the MCP server and
wires up both hooks -- no source checkout, no hand-editing of JSON:

~~~powershell
recall-admin install-hooks --agent claude-code
~~~

Add `--dry-run` first to preview every planned change (including the backup
file paths that would be created) without writing anything. The command is:

- **CLI-first**: it prefers `claude mcp add`/`claude mcp remove` when the
  `claude` CLI is on PATH, and falls back to a direct, validated edit of
  `~/.claude.json` otherwise.
- **Idempotent**: running it again after a successful install/migration is a
  no-op -- nothing is written and no backup is taken.
- **Non-destructive**: it validates JSON before writing, backs up any file it
  is about to modify, and never touches unrelated hooks, MCP entries, or
  top-level settings.
- **A migrator too**: if Claude Code already has a stale `remembrance` MCP
  entry or hook commands, they are migrated to `recall` in place, preserving
  the original directory, arguments, and environment variables. Pass
  `--force` to attempt a best-effort rename of an unrecognized hand-customized
  `remembrance` command; without it, unrecognized commands are left untouched
  with a warning.

Useful flags: `--scope user|local|project` (MCP registration scope, default
`user`), `--skip-mcp`, `--skip-hooks`, `--config-path`/`--settings-path` (point
at a scratch copy of `~/.claude.json`/`~/.claude/settings.json` instead of the
real ones -- mainly useful for testing).

The MCP server is registered pointing at `sys.executable -m recall_mcp`
rather than the bare `recall-mcp` console script: `pip install` does not
guarantee the interpreter's script directory is on PATH (a fresh Windows
venv, or pipx before `pipx ensurepath` has run), while `-m recall_mcp` needs
no PATH lookup at all.

Run `recall-admin install-hooks --help` for the full flag list, or see
`Get-Help .\integrations\claude-code\Install-ClaudeCodeIntegration.ps1 -Full`
for the (now deprecated, thin-wrapper) PowerShell entry point that still
accepts its original `-DryRun`/`-Force`/`-SkipMcp`/`-SkipHooks`/`-Scope`
parameter names.

## Manual setup

If you would rather wire things up by hand (or are scripting an install where
`recall-admin` isn't available yet), the pieces below still work.

### Register the MCP server

~~~bash
claude mcp add recall --scope user -- recall-mcp
~~~

For a source checkout on Windows, use the canonical wrapper:

~~~powershell
claude mcp add recall --scope user -- D:\path\to\recall\integrations\claude-code\recall_mcp_stdio.cmd
~~~

The deprecated remembrance_mcp_stdio.cmd filename delegates to the canonical wrapper.

### Install hooks

The hook scripts (`inject_context.py`, `capture_transcript.py`) and their
shared helpers now ship inside the installed `recall_mcp` package at
`src/recall_mcp/integrations/claude_code/` and
`src/recall_mcp/integrations/compat_env.py` / `recall_client.py` -- `pip
install recall-mcp` is enough to get them; there is nothing left to copy
alongside the scripts.

Edit the paths in `settings.snippet.json` (shipped at
`src/recall_mcp/integrations/claude_code/settings.snippet.json`), then merge
its hooks table into either:

- ~/.claude/settings.json for all projects
- .claude/settings.json for one project

`integrations/claude-code/inject_context.py` and `capture_transcript.py`
still exist at their old checkout-relative paths as thin backward-compat
shims that delegate to the packaged implementation, so existing hook
registrations keep working -- but new installs should point at the packaged
module paths above.

The Stop hook stores cursors and temporary outbox files under the resolved Recall home. If only the legacy home exists, it keeps using that directory in place.

## Environment

| Variable | Default |
| --- | --- |
| RECALL_URL | http://127.0.0.1:18790 |
| RECALL_TIMEOUT | 30 seconds for capture, 6 seconds for injection |
| RECALL_INJECT_LIMIT | 8 |
| RECALL_HOME | safe home-resolution order |
| RECALL_USER_ID | unset |
| RECALL_WORKSPACE_ID | unset |
| RECALL_PROJECT_ID | unset |
| RECALL_REPOSITORY_ID | unset |

Matching REMEMBRANCE_* names remain lower-priority fallbacks during migration.

RECALL_USER_ID, RECALL_WORKSPACE_ID, RECALL_PROJECT_ID, and RECALL_REPOSITORY_ID
are all optional and pin the Recall scope identity used by this client; each
has a matching REMEMBRANCE_* legacy fallback.

The SessionStart hook now prefers CAG (context-aware generation) delivery via
`POST /v2/context/deliver` and falls back to the v1 keyword `/search` route
if the v2 endpoint is unavailable.