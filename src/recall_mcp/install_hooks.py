"""Cross-platform installer for Recall's agent hook/MCP wiring.

Backs `recall-admin install-hooks`. The goal: `pip install recall-mcp` plus
one command should be enough to wire up an agent (Claude Code, Codex) on any
OS, with no source checkout and no hand-editing of JSON/TOML.

This module ports (and then supersedes) the logic in
``integrations/claude-code/Install-ClaudeCodeIntegration.ps1``. It inherits
that script's safety properties -- CLI-first with a direct-JSON fallback,
detection by reading config files directly (never by parsing CLI text
output), validate-before-write, timestamped backups, idempotency, a
non-clobbering hook-array merge, and remembrance -> recall migration -- and
two bugs found and fixed during that script's development:

- false-idempotency: the old script used to back up a file even when a run
  turned out to be a no-op. Backups are only taken immediately before a real
  write.
- double-nested hook arrays: a PowerShell pipeline auto-unwrap quirk could
  wrap a freshly merged hook-array in an extra array layer. Not applicable
  to this Python port (plain list objects, no pipeline unwrapping), but the
  merge logic is structured the same way so the invariant -- one flat list
  of hook groups per hook name -- is easy to keep true by inspection.

Design note on CLI-first vs. explicit paths: when a caller passes
``--config-path``/``--settings-path`` to point at a non-default file (as
tests do, and as anyone testing this against a scratch copy should do), the
``claude``/``codex`` CLI cannot be told to operate on that file -- both CLIs
always read/write the real per-user config. Invoking them in that situation
would silently do nothing useful and, worse, would touch the real config
out from under an explicit override. So: CLI-first mutation is only
attempted against the *default* config/settings locations; an explicit
path override always takes the direct-JSON path. This is what makes it safe
to test this module's write paths with `tmp_path` while a real `claude`/
`codex` CLI happens to be installed on the machine running the tests.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from recall_mcp.integrations import asset_path

SESSION_START_MARKER = "inject_context.py"
STOP_MARKER = "capture_transcript.py"
CODEX_SESSION_START_MARKER = "recall_session.py"
CODEX_STOP_MARKER = "capture_turn.py"

_CLI_TIMEOUT_SECONDS = 20


class InstallHooksError(Exception):
    """Raised to abort a single agent's install cleanly (no partial writes)."""


@dataclass
class StepResult:
    """Outcome of one migration/install step (MCP registration or hooks)."""

    status: str  # "no_op" | "created" | "updated" | "skipped" | "dry_run"
    detail: str
    backups: list[str] = field(default_factory=list)


@dataclass
class AgentResult:
    agent: str
    mcp: StepResult | None = None
    hooks: StepResult | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"agent": self.agent, "warnings": self.warnings}
        if self.mcp is not None:
            out["mcp"] = {
                "status": self.mcp.status,
                "detail": self.mcp.detail,
                "backups": self.mcp.backups,
            }
        if self.hooks is not None:
            out["hooks"] = {
                "status": self.hooks.status,
                "detail": self.hooks.detail,
                "backups": self.hooks.backups,
            }
        return out


def _plan(message: str) -> None:
    print(f"  [plan] {message}")


def _action(message: str) -> None:
    print(f"  [done] {message}")


def _warn(message: str) -> None:
    print(f"  [warn] {message}")


def _info(message: str) -> None:
    print(f"  {message}")


# ---------------------------------------------------------------------------
# Shared JSON helpers
# ---------------------------------------------------------------------------


def recall_invocation() -> list[str]:
    """Return the argv prefix used to launch the Recall MCP stdio server.

    Deliberately ``[sys.executable, "-m", "recall_mcp"]`` rather than the
    bare ``recall-mcp`` console script. `pip install` does not guarantee the
    interpreter's script directory is on PATH -- notably a fresh Windows
    venv, or pipx before ``pipx ensurepath`` has been run and a new shell
    opened. ``sys.executable -m recall_mcp`` needs no PATH lookup at all: it
    works identically for a plain venv, pipx, and a system install, at the
    (trivial) cost of a longer command line. This also matches how
    ``recall_mcp_stdio.cmd`` already invokes the server relative to its own
    known-good interpreter, so the invocation style is consistent across
    every installation shape this project supports.
    """

    return [sys.executable, "-m", "recall_mcp"]


def _load_json(path: Path) -> tuple[Any, bool]:
    """Return (parsed_json, existed). Raises InstallHooksError on bad JSON."""

    if not path.exists():
        return {}, False
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}, True
    try:
        return json.loads(raw), True
    except json.JSONDecodeError as exc:
        raise InstallHooksError(
            f"Failed to parse JSON ('{path}'): {exc}. Aborting without making any changes."
        ) from exc


def _backup_file(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_path = path.with_name(path.name + f".bak-{stamp}")
    shutil.copy2(path, backup_path)
    _action(f"Backed up '{path}' to '{backup_path}'")
    return backup_path


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")


def _get_nested(obj: Any, segments: list[str]) -> Any:
    current = obj
    for seg in segments:
        if not isinstance(current, dict) or seg not in current:
            return None
        current = current[seg]
    return current


def _set_nested(obj: dict[str, Any], segments: list[str], value: Any) -> None:
    current = obj
    for seg in segments[:-1]:
        nested = current.get(seg)
        if not isinstance(nested, dict):
            nested = {}
            current[seg] = nested
        current = nested
    current[segments[-1]] = value


def _run_cli(argv: list[str]) -> subprocess.CompletedProcess[str] | None:
    """Run a CLI command non-interactively. Returns None if it could not run."""

    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _echo_cli_output(result: subprocess.CompletedProcess[str]) -> None:
    for stream in (result.stdout, result.stderr):
        for line in (stream or "").splitlines():
            if line.strip():
                _info(line)


# ---------------------------------------------------------------------------
# Command-string migration (remembrance -> recall)
# ---------------------------------------------------------------------------


def _basename(command: str | None) -> str | None:
    if not command:
        return None
    try:
        return Path(command).name
    except (OSError, ValueError):
        return command


def migrate_command(
    old_command: str | None,
    old_args: list[Any] | None,
    *,
    force: bool,
) -> tuple[str | None, list[Any] | None, bool]:
    """Return (new_command, new_args, recognized) for a stale remembrance entry.

    Recognized patterns, checked in order:

    1. A ``remembrance_mcp_stdio.cmd`` wrapper -> the sibling
       ``recall_mcp_stdio.cmd`` in the same directory. Only relevant for a
       source checkout, but preserved so an existing dev registration keeps
       working without hand-editing.
    2. The bare ``remembrance-mcp`` console script -> ``recall-mcp``.
    3. A module invocation ``... -m remembrance_mcp`` (verified against a
       real Codex ``config.toml`` in the wild) -> ``-m recall_mcp``, with
       the interpreter/command left untouched.
    4. With ``--force``, any command that merely contains "remembrance"
       somewhere gets a best-effort case-insensitive substring rename. This
       subsumes patterns 1-2 as a special case and is the last resort for
       hand-customized commands that still reference the old name.

    A command with no such reference is always left untouched, force or
    not -- there would be nothing safe to substitute.
    """

    if not old_command:
        return None, None, False

    basename = _basename(old_command)
    args = list(old_args) if old_args else []

    if basename == "remembrance_mcp_stdio.cmd":
        parent = str(Path(old_command).parent)
        new_command = (
            str(Path(parent) / "recall_mcp_stdio.cmd")
            if parent and parent != "."
            else "recall_mcp_stdio.cmd"
        )
        return new_command, args, True

    if old_command == "remembrance-mcp":
        return "recall-mcp", args, True

    if "-m" in args:
        idx = args.index("-m")
        if idx + 1 < len(args) and args[idx + 1] == "remembrance_mcp":
            new_args = list(args)
            new_args[idx + 1] = "recall_mcp"
            return old_command, new_args, True

    if force and re.search("remembrance", old_command, re.IGNORECASE):
        new_command = re.sub("remembrance", "recall", old_command, flags=re.IGNORECASE)
        return new_command, args, True

    return None, None, False


def _is_canonical_recall_entry(command: str | None, args: list[Any] | None) -> bool:
    if not command:
        return False
    invocation = recall_invocation()
    if command == invocation[0] and list(args or []) == invocation[1:]:
        return True
    basename = _basename(command)
    if basename == "recall_mcp_stdio.cmd":
        return True
    if command == "recall-mcp":
        return True
    return False


# ---------------------------------------------------------------------------
# Hook array merge (shared shape between Claude Code and Codex)
# ---------------------------------------------------------------------------


def merge_hook_array(
    existing: list[Any],
    new_entry: dict[str, Any],
    marker: str,
    hook_name: str,
) -> tuple[list[Any], bool]:
    """Update-in-place-by-marker, append-if-absent merge for one hook array.

    ``existing`` is a list of hook groups, each ``{"hooks": [...]}``. Only
    the inner hook whose ``command`` contains ``marker`` is ever touched;
    every other group and every other inner hook (third-party tools,
    unrelated agents) is copied through unchanged. If no matching inner
    hook is found in any group, a brand new group is appended -- the
    existing array is never replaced wholesale.
    """

    found = False
    changed = False
    result_groups: list[Any] = []
    for group in existing:
        if not isinstance(group, dict):
            result_groups.append(group)
            continue
        inner_hooks = group.get("hooks")
        if not isinstance(inner_hooks, list):
            result_groups.append(group)
            continue
        new_inner: list[Any] = []
        for inner in inner_hooks:
            if (
                not found
                and isinstance(inner, dict)
                and marker in str(inner.get("command", ""))
            ):
                found = True
                updated = dict(inner)
                updated.update(new_entry)
                if updated != inner:
                    _plan(
                        f"Update existing {hook_name} hook command to point at the canonical script"
                    )
                    new_inner.append(updated)
                    changed = True
                else:
                    _info(
                        f"{hook_name} hook already points at the canonical script; leaving as-is."
                    )
                    new_inner.append(inner)
            else:
                new_inner.append(inner)
        new_group = dict(group)
        new_group["hooks"] = new_inner
        result_groups.append(new_group)

    if not found:
        _plan(f"Append a new {hook_name} hook entry for the canonical script")
        result_groups.append({"hooks": [dict(new_entry)]})
        changed = True

    return result_groups, changed


# ---------------------------------------------------------------------------
# Claude Code: MCP registration
# ---------------------------------------------------------------------------


def _claude_mcp_location(
    scope: str, config_path: Path | None, cwd: Path
) -> tuple[Path, list[str]]:
    if scope == "project":
        return (config_path or (cwd / ".mcp.json")), ["mcpServers"]
    if scope == "local":
        return (config_path or (Path.home() / ".claude.json")), [
            "projects",
            str(cwd),
            "mcpServers",
        ]
    return (config_path or (Path.home() / ".claude.json")), ["mcpServers"]


def install_claude_mcp(
    *,
    scope: str,
    config_path: Path | None,
    cwd: Path,
    force: bool,
    dry_run: bool,
    claude_cli: str | None,
) -> StepResult:
    """``claude_cli`` must be the resolved path from ``shutil.which("claude")``
    (or ``None`` to force the direct-JSON path), never the bare string
    "claude" -- on Windows a plain ``subprocess.run(["claude", ...])`` will
    not reliably locate a PATHEXT-resolved ``.cmd``/``.bat`` shim without
    ``shell=True``, so the exact resolved executable path must be used as
    argv[0].
    """
    file_path, segments = _claude_mcp_location(scope, config_path, cwd)
    invocation = recall_invocation()

    root, existed = _load_json(file_path)
    if not existed:
        root = {}
    if not isinstance(root, dict):
        raise InstallHooksError(
            f"'{file_path}' does not contain a JSON object at its root."
        )

    servers = _get_nested(root, segments)
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        raise InstallHooksError(
            f"'{file_path}' has a non-object value at {'.'.join(segments)}; refusing to touch it."
        )

    has_remembrance = "remembrance" in servers
    has_recall = "recall" in servers
    recall_entry = servers.get("recall") or {}
    recall_canonical = has_recall and _is_canonical_recall_entry(
        recall_entry.get("command"), recall_entry.get("args")
    )

    def backup_if_needed(did_backup: bool) -> bool:
        if not did_backup and existed:
            _backup_file(file_path)
            return True
        return did_backup

    # Case 1: nothing registered under either name -- fresh install.
    if not has_remembrance and not has_recall:
        _plan(f"Register a fresh 'recall' MCP entry [{scope}]")
        if dry_run:
            return StepResult("dry_run", f"would register 'recall' at {file_path}")

        added_via_cli = False
        if claude_cli:
            add_argv = [
                claude_cli,
                "mcp",
                "add",
                "recall",
                "--scope",
                scope,
                "--",
            ] + invocation
            result = _run_cli(add_argv)
            if result is not None:
                _echo_cli_output(result)
                added_via_cli = result.returncode == 0
                if not added_via_cli:
                    _warn(
                        "claude mcp add reported a non-zero exit; falling back to a direct JSON edit."
                    )

        backed_up = False
        if not added_via_cli:
            backed_up = backup_if_needed(backed_up)
            servers["recall"] = {
                "type": "stdio",
                "command": invocation[0],
                "args": invocation[1:],
            }
            _set_nested(root, segments, servers)
            _write_json(file_path, root)
        _action(f"Registered 'recall' MCP entry [{scope}]")
        return StepResult(
            "created",
            f"registered 'recall' at {file_path}",
            [str(file_path)] if backed_up else [],
        )

    # Case 2: recall already canonical, no stale remembrance -- no-op.
    if has_recall and recall_canonical and not has_remembrance:
        return StepResult(
            "no_op", "'recall' is already registered with a canonical command."
        )

    # Case 3: recall registered but with an unrecognized command -- leave it.
    if has_recall and not recall_canonical:
        message = (
            f"'recall' is already registered but with an unrecognized command "
            f"('{recall_entry.get('command')}'). Leaving it untouched."
        )
        _warn(message)
        if has_remembrance:
            _warn(
                "A legacy 'remembrance' entry also exists but will NOT be removed "
                "automatically because the existing 'recall' entry is not the "
                "canonical wrapper. Remove it manually if it is no longer needed."
            )
        return StepResult("skipped", message)

    # Case 4: recall canonical AND remembrance still present -- drop the stale one.
    if has_recall and recall_canonical and has_remembrance:
        _plan(
            f"Remove superseded 'remembrance' MCP entry (recall already canonical) [{scope}]"
        )
        if dry_run:
            return StepResult("dry_run", "would remove superseded 'remembrance' entry")

        removed_via_cli = False
        if claude_cli:
            result = _run_cli(
                [claude_cli, "mcp", "remove", "remembrance", "--scope", scope]
            )
            if result is not None:
                _echo_cli_output(result)
                removed_via_cli = result.returncode == 0
                if not removed_via_cli:
                    _warn(
                        "claude mcp remove reported a non-zero exit; falling back to a direct JSON edit."
                    )

        backed_up = False
        if not removed_via_cli:
            backed_up = backup_if_needed(backed_up)
            del servers["remembrance"]
            _set_nested(root, segments, servers)
            _write_json(file_path, root)
        _action(f"Removed superseded 'remembrance' MCP entry [{scope}]")
        return StepResult(
            "updated",
            "removed superseded 'remembrance' entry",
            [str(file_path)] if backed_up else [],
        )

    # Case 5: remembrance present, recall absent (or not canonical, handled above) -- migrate.
    old_entry = servers.get("remembrance", {})
    new_command, new_args, recognized = migrate_command(
        old_entry.get("command"), old_entry.get("args"), force=force
    )
    if not recognized:
        message = (
            f"The 'remembrance' MCP entry has an unrecognized command "
            f"('{old_entry.get('command')}'). Skipping migration to avoid overwriting a "
            "hand-customized entry. Re-run with --force to attempt a best-effort rename "
            "if the command still references 'remembrance'."
        )
        _warn(message)
        return StepResult("skipped", message)
    assert new_command is not None  # guaranteed by `recognized` being True

    _plan(f"Migrate MCP entry: remembrance -> recall [{scope}]")
    _plan(f"  command: '{old_entry.get('command')}' -> '{new_command}'")
    if dry_run:
        return StepResult("dry_run", "would migrate 'remembrance' -> 'recall'")

    entry_type = old_entry.get("type") or "stdio"
    migrated_via_cli = False
    if claude_cli:
        env_args: list[str] = []
        for key, value in (old_entry.get("env") or {}).items():
            env_args += ["-e", f"{key}={value}"]
        trailing_args = list(new_args or [])
        _run_cli([claude_cli, "mcp", "remove", "remembrance", "--scope", scope])
        add_argv = (
            [claude_cli, "mcp", "add", "recall", "--scope", scope]
            + env_args
            + ["--", new_command]
            + trailing_args
        )
        result = _run_cli(add_argv)
        if result is not None:
            _echo_cli_output(result)
            migrated_via_cli = result.returncode == 0
            if not migrated_via_cli:
                _warn(
                    "claude mcp add reported a non-zero exit; falling back to a direct JSON edit to complete the migration."
                )

    backed_up = False
    if not migrated_via_cli:
        backed_up = backup_if_needed(backed_up)
        # Start from a copy of the old entry (not a fresh 4-field dict) so any
        # extra key beyond type/command/args/env -- present or future --
        # survives the migration instead of being silently dropped. This is
        # the same class of bug as the Codex per-tool sub-table loss this
        # module's Codex path now guards against, just with no known
        # exploitable field on Claude Code's mcpServers schema today.
        new_entry: dict[str, Any] = dict(old_entry)
        new_entry["type"] = entry_type
        new_entry["command"] = new_command
        if new_args:
            new_entry["args"] = new_args
        else:
            new_entry.pop("args", None)
        if not old_entry.get("env"):
            new_entry.pop("env", None)
        del servers["remembrance"]
        servers["recall"] = new_entry
        _set_nested(root, segments, servers)
        _write_json(file_path, root)
        if not claude_cli:
            _info(
                f"claude CLI not found; wrote the migrated entry directly to '{file_path}'."
            )
    _action(f"Migrated MCP entry: remembrance -> recall [{scope}]")
    return StepResult(
        "updated",
        "migrated 'remembrance' -> 'recall'",
        [str(file_path)] if backed_up else [],
    )


# ---------------------------------------------------------------------------
# Claude Code: hooks
# ---------------------------------------------------------------------------


def _claude_hook_command(hook_name: str) -> str:
    script_name = SESSION_START_MARKER if hook_name == "SessionStart" else STOP_MARKER
    script_path = asset_path("claude_code", script_name)
    return f'"{sys.executable}" "{script_path}"'


def install_claude_hooks(*, settings_path: Path, dry_run: bool) -> StepResult:
    session_cmd = _claude_hook_command("SessionStart")
    stop_cmd = _claude_hook_command("Stop")

    if not settings_path.exists():
        _plan(f"Create '{settings_path}' with SessionStart/Stop hooks for Recall")
        if dry_run:
            return StepResult("dry_run", f"would create {settings_path}")
        new_settings = {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": session_cmd}]}
                ],
                "Stop": [{"hooks": [{"type": "command", "command": stop_cmd}]}],
            }
        }
        _write_json(settings_path, new_settings)
        _action(f"Created '{settings_path}' with Recall hooks")
        return StepResult("created", f"created {settings_path}")

    root, _existed = _load_json(settings_path)
    if not isinstance(root, dict):
        raise InstallHooksError(
            f"'{settings_path}' does not contain a JSON object at its root."
        )

    if "hooks" not in root or not isinstance(root.get("hooks"), dict):
        _plan("Add a 'hooks' property with SessionStart/Stop entries for Recall")
        if dry_run:
            return StepResult("dry_run", "would add a 'hooks' property")
        _backup_file(settings_path)
        root["hooks"] = {
            "SessionStart": [{"hooks": [{"type": "command", "command": session_cmd}]}],
            "Stop": [{"hooks": [{"type": "command", "command": stop_cmd}]}],
        }
        _write_json(settings_path, root)
        _action("Added 'hooks' with Recall SessionStart/Stop entries")
        return StepResult("updated", "added 'hooks' property", [str(settings_path)])

    hooks_obj = root["hooks"]
    backed_up = False
    changed = False

    for hook_name, marker, command in (
        ("SessionStart", SESSION_START_MARKER, session_cmd),
        ("Stop", STOP_MARKER, stop_cmd),
    ):
        existing_array = hooks_obj.get(hook_name)
        if not isinstance(existing_array, list):
            _plan(f"Add '{hook_name}' hook array for Recall")
            if dry_run:
                continue
            if not backed_up:
                _backup_file(settings_path)
                backed_up = True
            hooks_obj[hook_name] = [
                {"hooks": [{"type": "command", "command": command}]}
            ]
            changed = True
            continue

        if dry_run:
            # Still surface what would happen, without mutating anything.
            found = any(
                isinstance(inner, dict) and marker in str(inner.get("command", ""))
                for group in existing_array
                if isinstance(group, dict)
                for inner in group.get("hooks", [])
            )
            if not found:
                _plan(f"Append a new {hook_name} hook entry for the canonical script")
            continue

        merged, group_changed = merge_hook_array(
            existing_array, {"type": "command", "command": command}, marker, hook_name
        )
        if group_changed:
            if not backed_up:
                _backup_file(settings_path)
                backed_up = True
            hooks_obj[hook_name] = merged
            changed = True

    if dry_run:
        return StepResult(
            "dry_run", "would update hooks" if not backed_up else "no changes"
        )

    if changed:
        root["hooks"] = hooks_obj
        _write_json(settings_path, root)
        _action(f"Updated hooks in '{settings_path}'")
        return StepResult(
            "updated",
            f"updated hooks in {settings_path}",
            [str(settings_path)] if backed_up else [],
        )

    _info("Hooks already up to date; no changes needed.")
    return StepResult("no_op", "hooks already up to date")


# ---------------------------------------------------------------------------
# Codex: hooks (~/.codex/hooks.json)
# ---------------------------------------------------------------------------


def _codex_hook_command(hook_name: str) -> str:
    script_name = (
        CODEX_SESSION_START_MARKER if hook_name == "SessionStart" else CODEX_STOP_MARKER
    )
    script_path = asset_path("codex", script_name)
    return f'"{sys.executable}" "{script_path}"'


def _codex_hook_entry(hook_name: str) -> dict[str, Any]:
    return {
        "type": "command",
        "command": _codex_hook_command(hook_name),
        "timeoutSec": 10,
        "statusMessage": "Recalling Recall memory"
        if hook_name == "SessionStart"
        else "Capturing turn to Recall",
    }


def install_codex_hooks(*, hooks_path: Path, dry_run: bool) -> StepResult:
    if not hooks_path.exists():
        _plan(f"Create '{hooks_path}' with SessionStart/Stop hooks for Recall")
        if dry_run:
            return StepResult("dry_run", f"would create {hooks_path}")
        new_hooks = {
            "hooks": {
                "SessionStart": [{"hooks": [_codex_hook_entry("SessionStart")]}],
                "Stop": [{"hooks": [_codex_hook_entry("Stop")]}],
            }
        }
        _write_json(hooks_path, new_hooks)
        _action(f"Created '{hooks_path}' with Recall hooks")
        return StepResult("created", f"created {hooks_path}")

    root, _existed = _load_json(hooks_path)
    if not isinstance(root, dict):
        raise InstallHooksError(
            f"'{hooks_path}' does not contain a JSON object at its root."
        )

    if "hooks" not in root or not isinstance(root.get("hooks"), dict):
        _plan("Add a 'hooks' property with SessionStart/Stop entries for Recall")
        if dry_run:
            return StepResult("dry_run", "would add a 'hooks' property")
        _backup_file(hooks_path)
        root["hooks"] = {
            "SessionStart": [{"hooks": [_codex_hook_entry("SessionStart")]}],
            "Stop": [{"hooks": [_codex_hook_entry("Stop")]}],
        }
        _write_json(hooks_path, root)
        _action("Added 'hooks' with Recall SessionStart/Stop entries")
        return StepResult("updated", "added 'hooks' property", [str(hooks_path)])

    hooks_obj = root["hooks"]
    backed_up = False
    changed = False

    for hook_name, marker in (
        ("SessionStart", CODEX_SESSION_START_MARKER),
        ("Stop", CODEX_STOP_MARKER),
    ):
        entry = _codex_hook_entry(hook_name)
        existing_array = hooks_obj.get(hook_name)
        if not isinstance(existing_array, list):
            _plan(f"Add '{hook_name}' hook array for Recall")
            if dry_run:
                continue
            if not backed_up:
                _backup_file(hooks_path)
                backed_up = True
            hooks_obj[hook_name] = [{"hooks": [entry]}]
            changed = True
            continue

        if dry_run:
            found = any(
                isinstance(inner, dict) and marker in str(inner.get("command", ""))
                for group in existing_array
                if isinstance(group, dict)
                for inner in group.get("hooks", [])
            )
            if not found:
                _plan(f"Append a new {hook_name} hook entry for the canonical script")
            continue

        merged, group_changed = merge_hook_array(
            existing_array, entry, marker, hook_name
        )
        if group_changed:
            if not backed_up:
                _backup_file(hooks_path)
                backed_up = True
            hooks_obj[hook_name] = merged
            changed = True

    if dry_run:
        return StepResult(
            "dry_run", "would update hooks" if not backed_up else "no changes"
        )

    if changed:
        root["hooks"] = hooks_obj
        _write_json(hooks_path, root)
        _action(f"Updated hooks in '{hooks_path}'")
        return StepResult(
            "updated",
            f"updated hooks in {hooks_path}",
            [str(hooks_path)] if backed_up else [],
        )

    _info("Hooks already up to date; no changes needed.")
    return StepResult("no_op", "hooks already up to date")


# ---------------------------------------------------------------------------
# Codex: preserving per-tool config.toml sub-tables across a rename
# ---------------------------------------------------------------------------
#
# `codex mcp add`/`codex mcp remove` only know about the top-level
# `[mcp_servers.<name>]` table (command/args/env) -- verified against a real
# `codex mcp add --help`/`codex mcp remove --help`, neither of which has any
# flag for per-tool settings, and `-c key=value` is a runtime override for
# that invocation only, not something that gets persisted to config.toml (
# verified experimentally: `codex mcp add ... -c 'model="x"'` does not write
# `model` into the resulting config.toml at all). So a hand-customized
# `[mcp_servers.<name>.tools.<tool>]` sub-table (e.g. a per-tool
# `approval_mode`) has no CLI-expressible equivalent, and `codex mcp remove`
# deletes the entire dotted table -- sub-tables included -- out from under a
# `remembrance` -> `recall` rename.
#
# Recovering from that without a TOML *writer* dependency (the Python
# standard library has none, and this project doesn't otherwise need one --
# see the historical note this replaced) is possible because we don't need
# to interpret or reformat the sub-tables at all: we only need to carry
# their raw text across the rename with the parent table name swapped. This
# is a narrow, line-based text transform -- not a general TOML parser -- and
# is why it doesn't require `tomli`/`tomllib`/`tomlkit`/`tomli-w`.
#
# `install_codex_mcp` still goes through the `codex` CLI only for
# command/args/env (the part it *can* express); this module only ever reads
# config.toml directly to snapshot old sub-tables before a `remove`, and
# appends their renamed copies back after a successful `add`. Hooks (a real
# JSON file) are unaffected and still get the full merge/backup treatment in
# `install_codex_hooks`.


def _codex_config_toml_path() -> Path:
    """Resolve the same config.toml the `codex` CLI itself reads/writes.

    Mirrors the CLI's own resolution (verified experimentally against a real
    `codex` install via `CODEX_HOME`): `$CODEX_HOME/config.toml` when that
    env var is set, else `~/.codex/config.toml`.
    """

    codex_home = os.environ.get("CODEX_HOME")
    base = Path(codex_home).expanduser() if codex_home else (Path.home() / ".codex")
    return base / "config.toml"


_TOML_TABLE_HEADER_RE = re.compile(r"^\s*\[([^\[\]]+)\]\s*$")


def _extract_toml_subtables(text: str, parent_key: str) -> list[tuple[str, str]]:
    """Return ``[(suffix, raw_block_text), ...]`` for every plain
    ``[parent_key.suffix]`` table in ``text``, in file order.

    Deliberately not a general TOML parser: it only recognizes non-array
    table headers (``[[...]]`` is ignored) whose dotted path starts with
    ``parent_key.``, and captures each table's body byte-for-byte -- key
    order, comments, blank lines, quoting style and all -- from its header
    line up to the next ``[...]`` header (of *any* table, not just
    ``parent_key``'s) or end of file. ``parent_key`` itself (no suffix) is
    never matched, since that's the top-level entry `install_codex_mcp`
    already manages through the `codex` CLI.
    """

    prefix = parent_key + "."
    lines = text.splitlines(keepends=True)
    headers: list[tuple[int, str]] = []
    for idx, raw_line in enumerate(lines):
        match = _TOML_TABLE_HEADER_RE.match(raw_line.rstrip("\r\n"))
        if match:
            headers.append((idx, match.group(1).strip()))

    blocks: list[tuple[str, str]] = []
    for pos, (start_idx, key) in enumerate(headers):
        if not key.startswith(prefix):
            continue
        suffix = key[len(prefix) :]
        end_idx = headers[pos + 1][0] if pos + 1 < len(headers) else len(lines)
        blocks.append((suffix, "".join(lines[start_idx:end_idx])))
    return blocks


def _codex_toml_has_table(text: str, key: str) -> bool:
    for raw_line in text.splitlines():
        match = _TOML_TABLE_HEADER_RE.match(raw_line.rstrip("\r\n"))
        if match and match.group(1).strip() == key:
            return True
    return False


def _reapply_codex_subtables(
    config_toml_path: Path,
    subtables: list[tuple[str, str]],
    *,
    old_server: str,
    new_server: str,
) -> list[str]:
    """Append captured ``[mcp_servers.<old_server>.<suffix>]`` blocks back as
    ``[mcp_servers.<new_server>.<suffix>]``, skipping any that already exist
    under ``new_server`` (idempotency -- a re-run after a manual or prior
    partial fix must not duplicate a table). Returns the list of suffixes
    actually written; empty if there was nothing to do.

    Called *after* `codex mcp add` has already rewritten config.toml with
    the new top-level entry, so this only ever appends -- it never touches
    anything the CLI itself just wrote.
    """

    if not subtables:
        return []
    if not config_toml_path.exists():
        _warn(
            f"'{config_toml_path}' is missing after the codex CLI ran; cannot "
            "re-apply preserved per-tool sub-table(s). Re-add them manually."
        )
        return []
    try:
        current_text = config_toml_path.read_text(encoding="utf-8")
    except OSError as exc:
        _warn(
            f"Could not read '{config_toml_path}' to re-apply preserved per-tool "
            f"sub-table(s): {exc}. Re-add them manually."
        )
        return []

    pieces = [current_text]
    if current_text and not current_text.endswith("\n"):
        pieces.append("\n")
    appended: list[str] = []
    for suffix, block_text in subtables:
        new_key = f"mcp_servers.{new_server}.{suffix}"
        if _codex_toml_has_table(current_text, new_key):
            continue
        body_lines = block_text.splitlines(keepends=True)[1:]
        pieces.append(f"\n[{new_key}]\n" + "".join(body_lines))
        appended.append(suffix)

    if not appended:
        return []

    _backup_file(config_toml_path)
    config_toml_path.write_text("".join(pieces), encoding="utf-8")
    return appended


def _capture_codex_subtables_to_preserve(old_server: str) -> list[tuple[str, str]]:
    """Best-effort snapshot of ``[mcp_servers.<old_server>.*]`` sub-tables,
    taken before a `codex mcp remove` that would otherwise silently delete
    them. Returns ``[]`` (never raises) if config.toml doesn't exist or
    can't be read -- that's the same as there being nothing to preserve, and
    migration must not be blocked by it.
    """

    config_toml_path = _codex_config_toml_path()
    if not config_toml_path.exists():
        return []
    try:
        text = config_toml_path.read_text(encoding="utf-8")
    except OSError as exc:
        _warn(
            f"Could not read '{config_toml_path}' to check for per-tool "
            f"sub-tables to preserve: {exc}"
        )
        return []
    return _extract_toml_subtables(text, f"mcp_servers.{old_server}")


# ---------------------------------------------------------------------------
# Codex: MCP registration (CLI-first for command/args/env -- see the
# preservation helpers above for the one thing the CLI can't express)
# ---------------------------------------------------------------------------


def install_codex_mcp(
    *, force: bool, dry_run: bool, codex_cli: str | None
) -> StepResult:
    """Register/migrate the 'recall' MCP entry for Codex.

    ``codex_cli`` must be the resolved path from ``shutil.which("codex")``
    (or ``None``) -- see the equivalent note on `install_claude_mcp`.

    Codex stores MCP server registrations in ``~/.codex/config.toml`` (a
    ``[mcp_servers.<name>]`` table), verified directly against a real
    Codex installation -- not in a JSON file, unlike ``hooks.json``. Command/
    args/env go through the ``codex`` CLI only -- the Python standard
    library has no TOML *writer*, and hand-patching the *main* entry table
    well enough to satisfy this project's "validate before writing, never
    partially write" bar would mean taking on a new dependency or writing a
    bespoke TOML mutator neither asked for nor justified by that alone. When
    the CLI is unavailable, MCP registration is skipped with a clear warning
    rather than risking a corrupt config.toml -- hooks (a real JSON file)
    are unaffected and still get the full merge/backup treatment in
    `install_codex_hooks`.

    Per-tool ``[mcp_servers.<name>.tools.<tool>]`` sub-tables are a
    different story: the CLI has no way to express them at all (see the
    module section above this function), so a `remove` + `add` rename would
    silently drop them. Those are preserved with a narrow, format-preserving
    text transform on config.toml itself -- see `_capture_codex_subtables_to_preserve`
    / `_reapply_codex_subtables`.
    """

    if not codex_cli:
        message = (
            "codex CLI not found on PATH; skipping MCP server registration. "
            "Install the Codex CLI and re-run, or register manually with: "
            "codex mcp add recall -- " + " ".join(recall_invocation())
        )
        _warn(message)
        return StepResult("skipped", message)

    invocation = recall_invocation()

    # `codex mcp list --json` (verified against a real Codex install) returns
    # a JSON *array* of `{"name": ..., "transport": {"command", "args", ...}}`
    # objects, not a name-keyed object -- notably different from Claude
    # Code's `~/.claude.json`, which is why this parses `transport.command`/
    # `transport.args` rather than top-level fields.
    list_result = _run_cli([codex_cli, "mcp", "list", "--json"])
    servers: dict[str, dict[str, Any]] = {}
    if list_result is not None and list_result.returncode == 0:
        try:
            parsed = json.loads(list_result.stdout or "[]")
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list):
            for entry in parsed:
                if not isinstance(entry, dict) or not entry.get("name"):
                    continue
                transport = entry.get("transport") or {}
                servers[entry["name"]] = {
                    "command": transport.get("command"),
                    "args": transport.get("args") or [],
                    "env": transport.get("env"),
                }

    has_recall = "recall" in servers
    has_remembrance = "remembrance" in servers
    recall_entry = servers.get("recall") or {}
    recall_canonical = has_recall and _is_canonical_recall_entry(
        recall_entry.get("command"), recall_entry.get("args")
    )

    if not has_remembrance and not has_recall:
        _plan("Register a fresh 'recall' MCP entry for Codex")
        if dry_run:
            return StepResult(
                "dry_run", "would run: codex mcp add recall -- " + " ".join(invocation)
            )
        result = _run_cli([codex_cli, "mcp", "add", "recall", "--"] + invocation)
        if result is not None:
            _echo_cli_output(result)
        if result is None or result.returncode != 0:
            message = (
                "codex mcp add failed; register manually with: codex mcp add recall -- "
                + " ".join(invocation)
            )
            _warn(message)
            return StepResult("skipped", message)
        _action("Registered 'recall' MCP entry for Codex")
        return StepResult("created", "registered 'recall' via codex CLI")

    if has_recall and recall_canonical and not has_remembrance:
        return StepResult(
            "no_op", "'recall' is already registered with a canonical command."
        )

    if has_recall and not recall_canonical:
        message = (
            f"'recall' is already registered with Codex but with an unrecognized command "
            f"('{recall_entry.get('command')}'). Leaving it untouched."
        )
        _warn(message)
        return StepResult("skipped", message)

    if has_recall and recall_canonical and has_remembrance:
        _plan(
            "Remove superseded 'remembrance' MCP entry from Codex (recall already canonical)"
        )
        config_toml_path = _codex_config_toml_path()
        preserved = _capture_codex_subtables_to_preserve("remembrance")
        if dry_run:
            if preserved:
                names = ", ".join(f"mcp_servers.recall.{suffix}" for suffix, _ in preserved)
                return StepResult(
                    "dry_run",
                    "would remove superseded 'remembrance' entry "
                    f"(would preserve per-tool sub-table(s) as: {names})",
                )
            return StepResult("dry_run", "would remove superseded 'remembrance' entry")
        result = _run_cli([codex_cli, "mcp", "remove", "remembrance"])
        if result is not None:
            _echo_cli_output(result)
        if result is None or result.returncode != 0:
            message = "codex mcp remove failed for the superseded 'remembrance' entry; remove it manually."
            _warn(message)
            return StepResult("skipped", message)
        reapplied = _reapply_codex_subtables(
            config_toml_path, preserved, old_server="remembrance", new_server="recall"
        )
        if reapplied:
            _action(
                f"Preserved {len(reapplied)} per-tool sub-table(s) under 'recall': "
                + ", ".join(reapplied)
            )
        _action("Removed superseded 'remembrance' MCP entry from Codex")
        detail = "removed superseded 'remembrance' entry"
        if reapplied:
            detail += f" (preserved per-tool sub-table(s): {', '.join(reapplied)})"
        return StepResult("updated", detail)

    old_entry = servers.get("remembrance", {})
    new_command, new_args, recognized = migrate_command(
        old_entry.get("command"), old_entry.get("args"), force=force
    )
    if not recognized:
        message = (
            f"The 'remembrance' MCP entry has an unrecognized command "
            f"('{old_entry.get('command')}'). Skipping migration. Re-run with --force to "
            "attempt a best-effort rename if the command still references 'remembrance'."
        )
        _warn(message)
        return StepResult("skipped", message)

    _plan("Migrate Codex MCP entry: remembrance -> recall")
    config_toml_path = _codex_config_toml_path()
    preserved = _capture_codex_subtables_to_preserve("remembrance")
    if dry_run:
        if preserved:
            names = ", ".join(f"mcp_servers.recall.{suffix}" for suffix, _ in preserved)
            return StepResult(
                "dry_run",
                "would migrate 'remembrance' -> 'recall' "
                f"(would preserve per-tool sub-table(s) as: {names})",
            )
        return StepResult("dry_run", "would migrate 'remembrance' -> 'recall'")

    _run_cli([codex_cli, "mcp", "remove", "remembrance"])
    add_argv = (
        [codex_cli, "mcp", "add", "recall", "--"] + [new_command] + list(new_args or [])
    )
    result = _run_cli(add_argv)
    if result is not None:
        _echo_cli_output(result)
    if result is None or result.returncode != 0:
        message = (
            "codex mcp add failed while migrating; register manually with: codex mcp add recall -- "
            + " ".join(invocation)
        )
        _warn(message)
        return StepResult("skipped", message)
    reapplied = _reapply_codex_subtables(
        config_toml_path, preserved, old_server="remembrance", new_server="recall"
    )
    if reapplied:
        _action(
            f"Preserved {len(reapplied)} per-tool sub-table(s) under 'recall': "
            + ", ".join(reapplied)
        )
    _action("Migrated Codex MCP entry: remembrance -> recall")
    detail = "migrated 'remembrance' -> 'recall'"
    if reapplied:
        detail += f" (preserved per-tool sub-table(s): {', '.join(reapplied)})"
    return StepResult("updated", detail)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_install_hooks(
    *,
    agents: list[str],
    dry_run: bool,
    scope: str,
    force: bool,
    skip_mcp: bool,
    skip_hooks: bool,
    config_path: Path | None,
    settings_path: Path | None,
) -> dict[str, Any]:
    resolved_agents = (
        sorted({"claude-code", "codex"})
        if "all" in agents
        else list(dict.fromkeys(agents))
    )

    claude_cli = shutil.which("claude")
    codex_cli = shutil.which("codex")

    if dry_run:
        _info("DRY RUN: no files will be modified.")

    results: list[AgentResult] = []
    for agent in resolved_agents:
        _info("")
        _info(f"== {agent} ==")
        agent_result = AgentResult(agent=agent)

        if agent == "claude-code":
            using_default_config = config_path is None
            resolved_settings_path = settings_path or (
                Path.home() / ".claude" / "settings.json"
            )

            if not skip_mcp:
                try:
                    agent_result.mcp = install_claude_mcp(
                        scope=scope,
                        config_path=config_path,
                        cwd=Path.cwd(),
                        force=force,
                        dry_run=dry_run,
                        claude_cli=claude_cli if using_default_config else None,
                    )
                except InstallHooksError as exc:
                    agent_result.mcp = StepResult("error", str(exc))
                    agent_result.warnings.append(str(exc))
            if not skip_hooks:
                try:
                    agent_result.hooks = install_claude_hooks(
                        settings_path=resolved_settings_path, dry_run=dry_run
                    )
                except InstallHooksError as exc:
                    agent_result.hooks = StepResult("error", str(exc))
                    agent_result.warnings.append(str(exc))
            if not skip_mcp and not claude_cli:
                agent_result.warnings.append(
                    "claude CLI not found on PATH; used direct JSON edits for MCP registration."
                )
            elif not skip_mcp and not using_default_config:
                agent_result.warnings.append(
                    "--config-path was set; used direct JSON edits instead of the claude CLI."
                )

        elif agent == "codex":
            resolved_hooks_path = settings_path or (
                Path.home() / ".codex" / "hooks.json"
            )

            if not skip_mcp:
                try:
                    agent_result.mcp = install_codex_mcp(
                        force=force,
                        dry_run=dry_run,
                        codex_cli=codex_cli,
                    )
                except InstallHooksError as exc:
                    agent_result.mcp = StepResult("error", str(exc))
                    agent_result.warnings.append(str(exc))
            if not skip_hooks:
                try:
                    agent_result.hooks = install_codex_hooks(
                        hooks_path=resolved_hooks_path, dry_run=dry_run
                    )
                except InstallHooksError as exc:
                    agent_result.hooks = StepResult("error", str(exc))
                    agent_result.warnings.append(str(exc))
            if not skip_mcp and not codex_cli:
                agent_result.warnings.append(
                    "codex CLI not found on PATH; MCP server registration was skipped."
                )
            if not skip_mcp and config_path is not None:
                agent_result.warnings.append(
                    "--config-path has no effect for codex: MCP registration goes through the "
                    "codex CLI only (Codex stores MCP servers in config.toml, not JSON)."
                )

        else:
            agent_result.warnings.append(f"unknown agent '{agent}'; skipped.")

        results.append(agent_result)

    any_error = any(
        (r.mcp and r.mcp.status == "error") or (r.hooks and r.hooks.status == "error")
        for r in results
    )
    return {
        "dry_run": dry_run,
        "agents": [r.as_dict() for r in results],
        "ok": not any_error,
    }
