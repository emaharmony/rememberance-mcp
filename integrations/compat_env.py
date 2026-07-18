"""Shared environment compatibility for standalone Recall integrations."""

from __future__ import annotations

import os
import pathlib
import warnings


_warned = False


def _warn(legacy_name: str, canonical_name: str) -> None:
    global _warned
    if _warned:
        return
    warnings.warn(
        f"{legacy_name} is deprecated; use {canonical_name} instead.",
        FutureWarning,
        stacklevel=3,
    )
    _warned = True


def get_env(suffix: str, default: str | None = None) -> str | None:
    canonical_name = f"RECALL_{suffix}"
    legacy_name = f"REMEMBRANCE_{suffix}"
    if canonical_name in os.environ:
        return os.environ[canonical_name]
    if legacy_name in os.environ:
        _warn(legacy_name, canonical_name)
        return os.environ[legacy_name]
    return default


def resolve_home() -> pathlib.Path:
    explicit_recall = os.environ.get("RECALL_HOME")
    if explicit_recall:
        return pathlib.Path(explicit_recall).expanduser()

    home = pathlib.Path.home()
    recall_home = home / ".recall"
    if recall_home.exists():
        return recall_home

    explicit_legacy = os.environ.get("REMEMBRANCE_HOME")
    if explicit_legacy:
        _warn("REMEMBRANCE_HOME", "RECALL_HOME")
        return pathlib.Path(explicit_legacy).expanduser()

    legacy_home = home / ".remembrance"
    if legacy_home.exists():
        _warn("~/.remembrance", "~/.recall")
        return legacy_home

    return recall_home
