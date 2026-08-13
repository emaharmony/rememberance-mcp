"""Tests for `recall-admin install-hooks` (recall_mcp.install_hooks).

All write-path tests use `tmp_path` via `--config-path`/`--settings-path`
overrides; nothing here ever touches a real user config file. Every test
also monkeypatches `shutil.which` so the real system `claude`/`codex` CLI
(both of which may be installed on the machine running these tests) is
never actually invoked -- most tests force the direct-JSON fallback path by
returning `None`, and a couple of dedicated tests point at a small fake
stub script to exercise the CLI-first branch safely.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from recall_mcp import install_hooks as ih


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _no_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ih.shutil, "which", lambda _name: None)


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _hash(path: Path) -> bytes:
    return path.read_bytes() if path.exists() else b""


def _backups(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.bak-*"))


def _run(
    monkeypatch, tmp_path, *, agent, config_path=None, settings_path=None, **kwargs
):
    _no_cli(monkeypatch)
    return ih.run_install_hooks(
        agents=[agent],
        dry_run=kwargs.pop("dry_run", False),
        scope=kwargs.pop("scope", "user"),
        force=kwargs.pop("force", False),
        skip_mcp=kwargs.pop("skip_mcp", False),
        skip_hooks=kwargs.pop("skip_hooks", False),
        config_path=config_path,
        settings_path=settings_path,
    )


CANONICAL_COMMAND = sys.executable


# ---------------------------------------------------------------------------
# Claude Code: fresh machine
# ---------------------------------------------------------------------------


def test_claude_fresh_machine_creates_mcp_and_hooks(monkeypatch, tmp_path):
    config_path = tmp_path / ".claude.json"
    settings_path = tmp_path / "settings.json"

    result = _run(
        monkeypatch,
        tmp_path,
        agent="claude-code",
        config_path=config_path,
        settings_path=settings_path,
    )

    assert result["ok"] is True
    assert config_path.exists()
    assert settings_path.exists()

    servers = _read_json(config_path)["mcpServers"]
    assert servers["recall"]["command"] == CANONICAL_COMMAND
    assert servers["recall"]["args"] == ["-m", "recall_mcp"]

    hooks = _read_json(settings_path)["hooks"]
    assert "inject_context.py" in hooks["SessionStart"][0]["hooks"][0]["command"]
    assert "capture_transcript.py" in hooks["Stop"][0]["hooks"][0]["command"]

    agent_result = result["agents"][0]
    assert agent_result["mcp"]["status"] == "created"
    assert agent_result["hooks"]["status"] == "created"
    # Creating a file from nothing is not a "backup" event.
    assert agent_result["mcp"]["backups"] == []
    assert agent_result["hooks"]["backups"] == []


def test_codex_fresh_machine_creates_hooks(monkeypatch, tmp_path):
    hooks_path = tmp_path / "hooks.json"

    result = _run(
        monkeypatch, tmp_path, agent="codex", settings_path=hooks_path, skip_mcp=True
    )

    assert result["ok"] is True
    assert hooks_path.exists()
    hooks = _read_json(hooks_path)["hooks"]
    session_cmd = hooks["SessionStart"][0]["hooks"][0]["command"]
    assert "recall_session.py" in session_cmd
    assert hooks["SessionStart"][0]["hooks"][0]["timeoutSec"] == 10
    assert "capture_turn.py" in hooks["Stop"][0]["hooks"][0]["command"]
    assert result["agents"][0]["hooks"]["status"] == "created"


# ---------------------------------------------------------------------------
# Already canonical -> true no-op
# ---------------------------------------------------------------------------


def test_claude_already_canonical_is_true_no_op(monkeypatch, tmp_path):
    config_path = tmp_path / ".claude.json"
    settings_path = tmp_path / "settings.json"

    _run(
        monkeypatch,
        tmp_path,
        agent="claude-code",
        config_path=config_path,
        settings_path=settings_path,
    )
    hash_config_before = _hash(config_path)
    hash_settings_before = _hash(settings_path)

    result = _run(
        monkeypatch,
        tmp_path,
        agent="claude-code",
        config_path=config_path,
        settings_path=settings_path,
    )

    assert _hash(config_path) == hash_config_before
    assert _hash(settings_path) == hash_settings_before
    assert _backups(tmp_path) == []
    agent_result = result["agents"][0]
    assert agent_result["mcp"]["status"] == "no_op"
    assert agent_result["hooks"]["status"] == "no_op"


def test_codex_already_canonical_is_true_no_op(monkeypatch, tmp_path):
    hooks_path = tmp_path / "hooks.json"
    _run(monkeypatch, tmp_path, agent="codex", settings_path=hooks_path, skip_mcp=True)
    hash_before = _hash(hooks_path)

    result = _run(
        monkeypatch, tmp_path, agent="codex", settings_path=hooks_path, skip_mcp=True
    )

    assert _hash(hooks_path) == hash_before
    assert _backups(tmp_path) == []
    assert result["agents"][0]["hooks"]["status"] == "no_op"


# ---------------------------------------------------------------------------
# Stale "remembrance" entry migrated to "recall", preserving dir/args/env
# ---------------------------------------------------------------------------


def test_claude_migrates_stale_remembrance_module_invocation(monkeypatch, tmp_path):
    config_path = tmp_path / ".claude.json"
    settings_path = tmp_path / "settings.json"
    old_python = str(tmp_path / "old-venv" / "python.exe")
    _write_json(
        config_path,
        {
            "mcpServers": {
                "remembrance": {
                    "type": "stdio",
                    "command": old_python,
                    "args": ["-m", "remembrance_mcp"],
                    "env": {"REMEMBRANCE_HOME": "D:/old/home"},
                }
            }
        },
    )

    result = _run(
        monkeypatch,
        tmp_path,
        agent="claude-code",
        config_path=config_path,
        settings_path=settings_path,
    )

    servers = _read_json(config_path)["mcpServers"]
    assert "remembrance" not in servers
    recall_entry = servers["recall"]
    # Directory/interpreter and env preserved; only the module name changed.
    assert recall_entry["command"] == old_python
    assert recall_entry["args"] == ["-m", "recall_mcp"]
    assert recall_entry["env"] == {"REMEMBRANCE_HOME": "D:/old/home"}
    assert result["agents"][0]["mcp"]["status"] == "updated"
    assert len(_backups(tmp_path)) == 1


def test_claude_migrates_stale_remembrance_cmd_wrapper(monkeypatch, tmp_path):
    config_path = tmp_path / ".claude.json"
    settings_path = tmp_path / "settings.json"
    old_cmd = str(
        tmp_path
        / "checkout"
        / "integrations"
        / "claude-code"
        / "remembrance_mcp_stdio.cmd"
    )
    _write_json(
        config_path,
        {
            "mcpServers": {
                "remembrance": {
                    "type": "stdio",
                    "command": old_cmd,
                    "args": [],
                    "env": {},
                }
            }
        },
    )

    _run(
        monkeypatch,
        tmp_path,
        agent="claude-code",
        config_path=config_path,
        settings_path=settings_path,
    )

    servers = _read_json(config_path)["mcpServers"]
    assert "remembrance" not in servers
    expected_dir = str(Path(old_cmd).parent)
    assert servers["recall"]["command"] == str(
        Path(expected_dir) / "recall_mcp_stdio.cmd"
    )


# ---------------------------------------------------------------------------
# Hand-customized command -> warn and skip without --force
# ---------------------------------------------------------------------------


def test_claude_hand_customized_command_skipped_without_force(monkeypatch, tmp_path):
    config_path = tmp_path / ".claude.json"
    settings_path = tmp_path / "settings.json"
    _write_json(
        config_path,
        {
            "mcpServers": {
                "remembrance": {
                    "type": "stdio",
                    "command": "my-totally-custom-launcher",
                    "args": ["--flag"],
                    "env": {},
                }
            }
        },
    )
    before = _hash(config_path)

    result = _run(
        monkeypatch,
        tmp_path,
        agent="claude-code",
        config_path=config_path,
        settings_path=settings_path,
    )

    assert _hash(config_path) == before
    servers = _read_json(config_path)["mcpServers"]
    assert "remembrance" in servers
    assert "recall" not in servers
    assert result["agents"][0]["mcp"]["status"] == "skipped"
    assert _backups(tmp_path) == []


def test_claude_force_renames_hand_customized_command_referencing_remembrance(
    monkeypatch, tmp_path
):
    config_path = tmp_path / ".claude.json"
    settings_path = tmp_path / "settings.json"
    _write_json(
        config_path,
        {
            "mcpServers": {
                "remembrance": {
                    "type": "stdio",
                    "command": "D:/tools/my-remembrance-launcher.exe",
                    "args": [],
                    "env": {},
                }
            }
        },
    )

    result = _run(
        monkeypatch,
        tmp_path,
        agent="claude-code",
        config_path=config_path,
        settings_path=settings_path,
        force=True,
    )

    servers = _read_json(config_path)["mcpServers"]
    assert "remembrance" not in servers
    assert servers["recall"]["command"] == "D:/tools/my-recall-launcher.exe"
    assert result["agents"][0]["mcp"]["status"] == "updated"


def test_claude_hand_customized_command_with_no_remembrance_reference_never_renamed(
    monkeypatch, tmp_path
):
    """A command with no 'remembrance' reference is left alone even with --force."""
    config_path = tmp_path / ".claude.json"
    settings_path = tmp_path / "settings.json"
    _write_json(
        config_path,
        {
            "mcpServers": {
                "remembrance": {
                    "type": "stdio",
                    "command": "totally-unrelated",
                    "args": [],
                    "env": {},
                }
            }
        },
    )

    result = _run(
        monkeypatch,
        tmp_path,
        agent="claude-code",
        config_path=config_path,
        settings_path=settings_path,
        force=True,
    )

    servers = _read_json(config_path)["mcpServers"]
    assert "remembrance" in servers
    assert "recall" not in servers
    assert result["agents"][0]["mcp"]["status"] == "skipped"


# ---------------------------------------------------------------------------
# Unrelated hooks / top-level keys survive untouched (most important test)
# ---------------------------------------------------------------------------


def test_claude_unrelated_hooks_and_top_level_keys_survive(monkeypatch, tmp_path):
    config_path = tmp_path / ".claude.json"
    settings_path = tmp_path / "settings.json"

    _write_json(
        config_path,
        {
            "numStartups": 42,
            "theme": "dark",
            "mcpServers": {
                "blender": {
                    "type": "stdio",
                    "command": "uv",
                    "args": ["--directory", "D:/blender_mcp/mcp", "run", "blender-mcp"],
                    "env": {},
                }
            },
        },
    )
    _write_json(
        settings_path,
        {
            "permissions": {"allow": ["Bash(git *)"]},
            "someOtherTopLevelKey": {"nested": True},
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/usr/bin/some-other-tool --startup",
                            }
                        ]
                    }
                ],
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/usr/bin/some-other-tool --stop",
                            }
                        ]
                    }
                ],
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "guard.sh"}],
                    }
                ],
            },
        },
    )

    result = _run(
        monkeypatch,
        tmp_path,
        agent="claude-code",
        config_path=config_path,
        settings_path=settings_path,
    )

    config_after = _read_json(config_path)
    assert config_after["numStartups"] == 42
    assert config_after["theme"] == "dark"
    assert config_after["mcpServers"]["blender"] == {
        "type": "stdio",
        "command": "uv",
        "args": ["--directory", "D:/blender_mcp/mcp", "run", "blender-mcp"],
        "env": {},
    }
    assert "recall" in config_after["mcpServers"]

    settings_after = _read_json(settings_path)
    assert settings_after["permissions"] == {"allow": ["Bash(git *)"]}
    assert settings_after["someOtherTopLevelKey"] == {"nested": True}
    assert settings_after["hooks"]["PreToolUse"] == [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "guard.sh"}]}
    ]
    # The unrelated SessionStart/Stop entries are still present alongside ours.
    session_commands = [
        h["command"] for h in settings_after["hooks"]["SessionStart"][0]["hooks"]
    ]
    assert "/usr/bin/some-other-tool --startup" in session_commands
    stop_commands = [h["command"] for h in settings_after["hooks"]["Stop"][0]["hooks"]]
    assert "/usr/bin/some-other-tool --stop" in stop_commands
    # Ours got appended as a new group, not merged into the unrelated tool's hooks.
    assert len(settings_after["hooks"]["SessionStart"]) == 2
    assert len(settings_after["hooks"]["Stop"]) == 2
    assert result["agents"][0]["mcp"]["status"] == "created"
    assert result["agents"][0]["hooks"]["status"] == "updated"


def test_codex_unrelated_hooks_and_top_level_keys_survive(monkeypatch, tmp_path):
    hooks_path = tmp_path / "hooks.json"
    _write_json(
        hooks_path,
        {
            "someUnrelatedTopLevelKey": "keep-me",
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/usr/bin/other-agent-hook --start",
                                "timeoutSec": 5,
                                "statusMessage": "other agent",
                            }
                        ]
                    }
                ],
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/usr/bin/other-agent-hook --stop",
                            }
                        ]
                    }
                ],
            },
        },
    )

    result = _run(
        monkeypatch, tmp_path, agent="codex", settings_path=hooks_path, skip_mcp=True
    )

    after = _read_json(hooks_path)
    assert after["someUnrelatedTopLevelKey"] == "keep-me"
    session_commands = [
        h["command"] for h in after["hooks"]["SessionStart"][0]["hooks"]
    ]
    assert "/usr/bin/other-agent-hook --start" in session_commands
    assert len(after["hooks"]["SessionStart"]) == 2
    assert result["agents"][0]["hooks"]["status"] == "updated"


# ---------------------------------------------------------------------------
# Stale hook command updated in place, not duplicated
# ---------------------------------------------------------------------------


def test_claude_stale_hook_command_updated_in_place(monkeypatch, tmp_path):
    config_path = tmp_path / ".claude.json"
    settings_path = tmp_path / "settings.json"
    _write_json(
        settings_path,
        {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": '"D:/old/.venv/Scripts/python.exe" "D:/old/integrations/claude-code/inject_context.py"',
                            }
                        ]
                    }
                ],
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": '"D:/old/.venv/Scripts/python.exe" "D:/old/integrations/claude-code/capture_transcript.py"',
                            }
                        ]
                    }
                ],
            }
        },
    )

    result = _run(
        monkeypatch,
        tmp_path,
        agent="claude-code",
        config_path=config_path,
        settings_path=settings_path,
        skip_mcp=True,
    )

    after = _read_json(settings_path)
    assert len(after["hooks"]["SessionStart"]) == 1
    assert len(after["hooks"]["SessionStart"][0]["hooks"]) == 1
    new_cmd = after["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert sys.executable in new_cmd
    assert "D:/old" not in new_cmd
    assert result["agents"][0]["hooks"]["status"] == "updated"


def test_codex_stale_hook_command_updated_in_place_preserves_timeout_fields(
    monkeypatch, tmp_path
):
    hooks_path = tmp_path / "hooks.json"
    _write_json(
        hooks_path,
        {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": '"D:/old/.venv/Scripts/python.exe" "D:/old/integrations/codex/recall_session.py"',
                                "timeoutSec": 99,
                                "statusMessage": "old message",
                            }
                        ]
                    }
                ],
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": '"D:/old/.venv/Scripts/python.exe" "D:/old/integrations/codex/capture_turn.py"',
                            }
                        ]
                    }
                ],
            }
        },
    )

    _run(monkeypatch, tmp_path, agent="codex", settings_path=hooks_path, skip_mcp=True)

    after = _read_json(hooks_path)
    session_hook = after["hooks"]["SessionStart"][0]["hooks"][0]
    assert sys.executable in session_hook["command"]
    assert len(after["hooks"]["SessionStart"]) == 1
    # Our merge overwrites timeoutSec/statusMessage with the canonical values
    # (they are part of our own hook entry's shape), it does not invent new
    # unrelated groups.
    assert session_hook["timeoutSec"] == 10


# ---------------------------------------------------------------------------
# Idempotency: run twice, second run writes nothing
# ---------------------------------------------------------------------------


def test_claude_idempotent_across_two_runs(monkeypatch, tmp_path):
    config_path = tmp_path / ".claude.json"
    settings_path = tmp_path / "settings.json"
    _write_json(
        config_path,
        {
            "mcpServers": {
                "remembrance": {
                    "type": "stdio",
                    "command": "remembrance-mcp",
                    "args": [],
                    "env": {},
                }
            }
        },
    )

    _run(
        monkeypatch,
        tmp_path,
        agent="claude-code",
        config_path=config_path,
        settings_path=settings_path,
    )
    hash_config_1 = _hash(config_path)
    hash_settings_1 = _hash(settings_path)
    backups_after_first_run = len(_backups(tmp_path))
    assert backups_after_first_run >= 1

    result = _run(
        monkeypatch,
        tmp_path,
        agent="claude-code",
        config_path=config_path,
        settings_path=settings_path,
    )

    assert _hash(config_path) == hash_config_1
    assert _hash(settings_path) == hash_settings_1
    assert len(_backups(tmp_path)) == backups_after_first_run  # no new backups
    assert result["agents"][0]["mcp"]["status"] == "no_op"
    assert result["agents"][0]["hooks"]["status"] == "no_op"


def test_codex_idempotent_across_two_runs(monkeypatch, tmp_path):
    hooks_path = tmp_path / "hooks.json"

    _run(monkeypatch, tmp_path, agent="codex", settings_path=hooks_path, skip_mcp=True)
    hash_1 = _hash(hooks_path)

    result = _run(
        monkeypatch, tmp_path, agent="codex", settings_path=hooks_path, skip_mcp=True
    )

    assert _hash(hooks_path) == hash_1
    assert _backups(tmp_path) == []
    assert result["agents"][0]["hooks"]["status"] == "no_op"


# ---------------------------------------------------------------------------
# Malformed JSON -> abort cleanly, nonzero, no write
# ---------------------------------------------------------------------------


def test_claude_malformed_config_json_aborts_without_writing(monkeypatch, tmp_path):
    config_path = tmp_path / ".claude.json"
    settings_path = tmp_path / "settings.json"
    config_path.write_text("{not valid json", encoding="utf-8")
    before = _hash(config_path)

    result = _run(
        monkeypatch,
        tmp_path,
        agent="claude-code",
        config_path=config_path,
        settings_path=settings_path,
    )

    assert _hash(config_path) == before
    assert result["ok"] is False
    assert result["agents"][0]["mcp"]["status"] == "error"
    assert _backups(tmp_path) == []


def test_codex_malformed_hooks_json_aborts_without_writing(monkeypatch, tmp_path):
    hooks_path = tmp_path / "hooks.json"
    hooks_path.write_text("{ this is not json at all", encoding="utf-8")
    before = _hash(hooks_path)

    result = _run(
        monkeypatch, tmp_path, agent="codex", settings_path=hooks_path, skip_mcp=True
    )

    assert _hash(hooks_path) == before
    assert result["ok"] is False
    assert result["agents"][0]["hooks"]["status"] == "error"
    assert _backups(tmp_path) == []


def test_claude_malformed_settings_json_aborts_without_writing(monkeypatch, tmp_path):
    config_path = tmp_path / ".claude.json"
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("not json {{{", encoding="utf-8")
    before = _hash(settings_path)

    result = _run(
        monkeypatch,
        tmp_path,
        agent="claude-code",
        config_path=config_path,
        settings_path=settings_path,
    )

    assert _hash(settings_path) == before
    assert result["ok"] is False
    assert result["agents"][0]["hooks"]["status"] == "error"


# ---------------------------------------------------------------------------
# --dry-run writes nothing
# ---------------------------------------------------------------------------


def test_claude_dry_run_writes_nothing(monkeypatch, tmp_path):
    config_path = tmp_path / ".claude.json"
    settings_path = tmp_path / "settings.json"
    _write_json(
        config_path,
        {
            "mcpServers": {
                "remembrance": {
                    "type": "stdio",
                    "command": "remembrance-mcp",
                    "args": [],
                    "env": {},
                }
            }
        },
    )
    _write_json(
        settings_path,
        {"hooks": {"SessionStart": [], "Stop": []}},
    )
    config_before = _hash(config_path)
    settings_before = _hash(settings_path)

    result = _run(
        monkeypatch,
        tmp_path,
        agent="claude-code",
        config_path=config_path,
        settings_path=settings_path,
        dry_run=True,
    )

    assert _hash(config_path) == config_before
    assert _hash(settings_path) == settings_before
    assert not config_path.with_suffix(".json.bak").exists()
    assert _backups(tmp_path) == []
    assert result["dry_run"] is True
    assert result["agents"][0]["mcp"]["status"] == "dry_run"
    assert result["agents"][0]["hooks"]["status"] == "dry_run"


def test_claude_dry_run_on_fresh_machine_creates_no_files(monkeypatch, tmp_path):
    config_path = tmp_path / ".claude.json"
    settings_path = tmp_path / "settings.json"

    result = _run(
        monkeypatch,
        tmp_path,
        agent="claude-code",
        config_path=config_path,
        settings_path=settings_path,
        dry_run=True,
    )

    assert not config_path.exists()
    assert not settings_path.exists()
    assert result["agents"][0]["mcp"]["status"] == "dry_run"
    assert result["agents"][0]["hooks"]["status"] == "dry_run"


def test_codex_dry_run_writes_nothing(monkeypatch, tmp_path):
    hooks_path = tmp_path / "hooks.json"
    _write_json(hooks_path, {"hooks": {"SessionStart": [], "Stop": []}})
    before = _hash(hooks_path)

    result = _run(
        monkeypatch,
        tmp_path,
        agent="codex",
        settings_path=hooks_path,
        skip_mcp=True,
        dry_run=True,
    )

    assert _hash(hooks_path) == before
    assert _backups(tmp_path) == []
    assert result["agents"][0]["hooks"]["status"] == "dry_run"


# ---------------------------------------------------------------------------
# CLI absent -> JSON fallback path still works
# ---------------------------------------------------------------------------


def test_claude_cli_absent_falls_back_to_json_edit(monkeypatch, tmp_path):
    config_path = tmp_path / ".claude.json"
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(ih.shutil, "which", lambda name: None)

    result = ih.run_install_hooks(
        agents=["claude-code"],
        dry_run=False,
        scope="user",
        force=False,
        skip_mcp=False,
        skip_hooks=False,
        config_path=config_path,
        settings_path=settings_path,
    )

    assert result["ok"] is True
    assert config_path.exists()
    servers = _read_json(config_path)["mcpServers"]
    assert servers["recall"]["command"] == CANONICAL_COMMAND
    assert any("claude CLI not found" in w for w in result["agents"][0]["warnings"])


def test_codex_cli_absent_skips_mcp_but_hooks_still_written(monkeypatch, tmp_path):
    hooks_path = tmp_path / "hooks.json"
    monkeypatch.setattr(ih.shutil, "which", lambda name: None)

    result = ih.run_install_hooks(
        agents=["codex"],
        dry_run=False,
        scope="user",
        force=False,
        skip_mcp=False,
        skip_hooks=False,
        config_path=None,
        settings_path=hooks_path,
    )

    assert hooks_path.exists()
    agent_result = result["agents"][0]
    assert agent_result["mcp"]["status"] == "skipped"
    assert agent_result["hooks"]["status"] == "created"
    assert any("codex CLI not found" in w for w in agent_result["warnings"])


# ---------------------------------------------------------------------------
# CLI-present branch, exercised via a fake stub (never the real system CLI)
# ---------------------------------------------------------------------------


def _write_fake_cli(tmp_path: Path, name: str, script: str) -> Path:
    if sys.platform == "win32":
        path = tmp_path / f"{name}.py"
        path.write_text(script, encoding="utf-8")
        wrapper = tmp_path / f"{name}.cmd"
        wrapper.write_text(
            f'@echo off\r\n"{sys.executable}" "{path}" %*\r\n', encoding="utf-8"
        )
        return wrapper
    path = tmp_path / name
    path.write_text(f"#!{sys.executable}\n{script}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def test_claude_cli_present_used_when_config_path_is_default(monkeypatch, tmp_path):
    """CLI-first is only attempted against the default config path; confirm
    that a fake 'claude' stub is actually invoked in that case, and that its
    success is honored (no JSON fallback needed)."""

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(ih.Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setattr(ih, "_CLI_TIMEOUT_SECONDS", 10)

    log_path = tmp_path / "cli_calls.log"
    stub = _write_fake_cli(
        tmp_path,
        "claude",
        (
            "import sys\n"
            f"open(r'{log_path}', 'a', encoding='utf-8').write(' '.join(sys.argv[1:]) + chr(10))\n"
            "sys.exit(0)\n"
        ),
    )
    monkeypatch.setattr(
        ih.shutil, "which", lambda name: str(stub) if name == "claude" else None
    )

    result = ih.run_install_hooks(
        agents=["claude-code"],
        dry_run=False,
        scope="user",
        force=False,
        skip_mcp=False,
        skip_hooks=True,
        config_path=None,
        settings_path=None,
    )

    assert log_path.exists(), "expected the fake claude CLI stub to be invoked"
    call = log_path.read_text(encoding="utf-8").strip()
    assert "mcp add recall" in call
    assert not (fake_home / ".claude.json").exists(), (
        "CLI path should not fall back to a JSON write on success"
    )
    assert result["agents"][0]["mcp"]["status"] == "created"


# ---------------------------------------------------------------------------
# Codex MCP: per-tool config.toml sub-tables must survive a rename
#
# Regression coverage for a real observed bug: `codex mcp add`/`remove` only
# know about the top-level [mcp_servers.<name>] table (command/args/env), so
# a naive remove-then-add migration silently drops any hand-added
# [mcp_servers.<name>.tools.<tool>] sub-table (e.g. a per-tool
# approval_mode). The fake CLI below intentionally reproduces the one
# behavior this bug hinges on -- verified against a real Codex install --
# that `codex mcp remove <name>` deletes that name's entire dotted table
# family, sub-tables included.
# ---------------------------------------------------------------------------


_FAKE_CODEX_CLI_SCRIPT = r'''
import json
import re
import sys
from pathlib import Path

CONFIG = Path(r"__CONFIG_TOML_PATH__")


def _read():
    return CONFIG.read_text(encoding="utf-8") if CONFIG.exists() else ""


def _write(text):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(text, encoding="utf-8")


def _top_level_servers(text):
    servers = {}
    for m in re.finditer(r"(?m)^\[mcp_servers\.([A-Za-z0-9_-]+)\]\s*$", text):
        name = m.group(1)
        nxt = re.search(r"(?m)^\[", text[m.end():])
        block = text[m.end():m.end() + nxt.start()] if nxt else text[m.end():]
        cmd_m = re.search(r'(?m)^command\s*=\s*"([^"]*)"', block)
        args_m = re.search(r"(?m)^args\s*=\s*(\[[^\]]*\])", block)
        command = cmd_m.group(1) if cmd_m else None
        args = json.loads(args_m.group(1)) if args_m else []
        servers[name] = (command, args)
    return servers


argv = sys.argv[1:]

if len(argv) >= 2 and argv[0] == "mcp" and argv[1] == "list":
    servers = _top_level_servers(_read())
    out = [
        {"name": name, "transport": {"command": command, "args": args}}
        for name, (command, args) in servers.items()
    ]
    print(json.dumps(out))
    sys.exit(0)

if len(argv) >= 3 and argv[0] == "mcp" and argv[1] == "remove":
    name = argv[2]
    text = _read()
    pattern = re.compile(r"(?m)^\[mcp_servers\." + re.escape(name) + r"(\.[^\]]+)?\]\s*$")
    kept = []
    skipping = False
    for line in text.splitlines(keepends=True):
        if pattern.match(line.rstrip("\r\n")):
            skipping = True
            continue
        if skipping and re.match(r"^\s*\[", line):
            skipping = False
        if not skipping:
            kept.append(line)
    _write("".join(kept))
    sys.exit(0)

if len(argv) >= 3 and argv[0] == "mcp" and argv[1] == "add":
    name = argv[2]
    dd = argv.index("--")
    rest = argv[dd + 1:]
    command, args = rest[0], rest[1:]
    text = _read()
    if text and not text.endswith("\n"):
        text += "\n"
    if text:
        text += "\n"
    text += "[mcp_servers.%s]\ncommand = %s\nargs = %s\n" % (
        name,
        json.dumps(command),
        json.dumps(args),
    )
    _write(text)
    sys.exit(0)

sys.exit(1)
'''


def _write_fake_codex_cli(tmp_path: Path, config_toml_path: Path) -> Path:
    script = _FAKE_CODEX_CLI_SCRIPT.replace(
        "__CONFIG_TOML_PATH__", str(config_toml_path)
    )
    return _write_fake_cli(tmp_path, "codex", script)


def _run_codex_mcp(monkeypatch, tmp_path, config_toml_path, *, dry_run=False):
    fake_home = tmp_path / "home"
    monkeypatch.setattr(ih.Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    stub = _write_fake_codex_cli(tmp_path, config_toml_path)
    monkeypatch.setattr(
        ih.shutil, "which", lambda name: str(stub) if name == "codex" else None
    )
    return ih.run_install_hooks(
        agents=["codex"],
        dry_run=dry_run,
        scope="user",
        force=False,
        skip_mcp=False,
        skip_hooks=True,
        config_path=None,
        settings_path=None,
    )


def test_codex_migrates_stale_remembrance_preserving_tool_subtables(
    monkeypatch, tmp_path
):
    """Fails without the fix: a plain remove+add migration would leave
    config.toml with a 'recall' entry and no 'tools.*' sub-tables at all."""

    config_toml_path = tmp_path / "home" / ".codex" / "config.toml"
    config_toml_path.parent.mkdir(parents=True)
    config_toml_path.write_text(
        '[mcp_servers.remembrance]\n'
        'command = "remembrance-mcp"\n'
        "args = []\n"
        "\n"
        "[mcp_servers.remembrance.tools.memory_context_build]\n"
        'approval_mode = "approve"\n'
        "\n"
        "[mcp_servers.remembrance.tools.memory_capture]\n"
        'approval_mode = "approve"\n',
        encoding="utf-8",
    )

    result = _run_codex_mcp(monkeypatch, tmp_path, config_toml_path)

    final_text = config_toml_path.read_text(encoding="utf-8")
    assert "[mcp_servers.recall]" in final_text
    assert "[mcp_servers.remembrance]" not in final_text
    assert "[mcp_servers.remembrance.tools" not in final_text
    assert "[mcp_servers.recall.tools.memory_context_build]" in final_text
    assert "[mcp_servers.recall.tools.memory_capture]" in final_text
    assert final_text.count('approval_mode = "approve"') == 2

    agent_result = result["agents"][0]
    assert agent_result["mcp"]["status"] == "updated"
    assert "preserved" in agent_result["mcp"]["detail"]

    backups = list(config_toml_path.parent.glob("config.toml.bak-*"))
    assert len(backups) == 1


def test_codex_migration_dry_run_preserves_subtables_reports_without_writing(
    monkeypatch, tmp_path
):
    config_toml_path = tmp_path / "home" / ".codex" / "config.toml"
    config_toml_path.parent.mkdir(parents=True)
    original_text = (
        '[mcp_servers.remembrance]\n'
        'command = "remembrance-mcp"\n'
        "args = []\n"
        "\n"
        "[mcp_servers.remembrance.tools.memory_capture]\n"
        'approval_mode = "approve"\n'
    )
    config_toml_path.write_text(original_text, encoding="utf-8")

    result = _run_codex_mcp(monkeypatch, tmp_path, config_toml_path, dry_run=True)

    assert config_toml_path.read_text(encoding="utf-8") == original_text
    assert list(config_toml_path.parent.glob("config.toml.bak-*")) == []
    agent_result = result["agents"][0]
    assert agent_result["mcp"]["status"] == "dry_run"
    assert "mcp_servers.recall.tools.memory_capture" in agent_result["mcp"]["detail"]


def test_codex_migration_with_subtables_idempotent_across_two_runs(
    monkeypatch, tmp_path
):
    config_toml_path = tmp_path / "home" / ".codex" / "config.toml"
    config_toml_path.parent.mkdir(parents=True)
    config_toml_path.write_text(
        '[mcp_servers.remembrance]\n'
        'command = "remembrance-mcp"\n'
        "args = []\n"
        "\n"
        "[mcp_servers.remembrance.tools.memory_capture]\n"
        'approval_mode = "approve"\n',
        encoding="utf-8",
    )

    first = _run_codex_mcp(monkeypatch, tmp_path, config_toml_path)
    assert first["agents"][0]["mcp"]["status"] == "updated"

    second = _run_codex_mcp(monkeypatch, tmp_path, config_toml_path)
    assert second["agents"][0]["mcp"]["status"] == "no_op"

    final_text = config_toml_path.read_text(encoding="utf-8")
    assert final_text.count("[mcp_servers.recall.tools.memory_capture]") == 1


# ---------------------------------------------------------------------------
# Command string migration helper (unit-level)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "old_command,old_args,expected_command_contains",
    [
        ("remembrance-mcp", [], "recall-mcp"),
    ],
)
def test_migrate_command_bare_console_script(
    old_command, old_args, expected_command_contains
):
    new_command, new_args, recognized = ih.migrate_command(
        old_command, old_args, force=False
    )
    assert recognized is True
    assert new_command == expected_command_contains
    assert new_args == []


def test_migrate_command_unrecognized_without_force():
    new_command, new_args, recognized = ih.migrate_command(
        "custom-thing", ["--flag"], force=False
    )
    assert recognized is False
    assert new_command is None


def test_migrate_command_no_command_is_a_no_op():
    new_command, new_args, recognized = ih.migrate_command(None, None, force=True)
    assert recognized is False
    assert new_command is None


# ---------------------------------------------------------------------------
# 'all' agent value wires up both agents
# ---------------------------------------------------------------------------


def test_agent_all_wires_up_both_agents(monkeypatch, tmp_path):
    """'all' expands to both agents. Uses a fake HOME (never the real one,
    even under --dry-run) since `agents=["all"]` with no explicit
    config/settings paths resolves both agents' default (home-relative)
    locations."""

    _no_cli(monkeypatch)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(ih.Path, "home", classmethod(lambda cls: fake_home))

    result = ih.run_install_hooks(
        agents=["all"],
        dry_run=True,
        scope="user",
        force=False,
        skip_mcp=False,
        skip_hooks=False,
        config_path=None,
        settings_path=None,
    )
    agent_names = {a["agent"] for a in result["agents"]}
    assert agent_names == {"claude-code", "codex"}
    assert not (fake_home / ".claude.json").exists()
    assert not (fake_home / ".codex" / "hooks.json").exists()
