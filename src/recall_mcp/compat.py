"""Compatibility helpers for the Remembrance-to-Recall migration."""

from __future__ import annotations

import os
import warnings
from pathlib import Path


class RecallCompatibilityWarning(FutureWarning):
    """Warning emitted when a deprecated Remembrance interface is used."""


_legacy_env_warning_emitted = False
_legacy_home_warning_emitted = False


def _warn_legacy_env(canonical_name: str, legacy_name: str) -> None:
    global _legacy_env_warning_emitted
    if _legacy_env_warning_emitted:
        return
    warnings.warn(
        f"{legacy_name} is deprecated; use {canonical_name} instead. "
        "The legacy value remains supported for this migration period.",
        RecallCompatibilityWarning,
        stacklevel=3,
    )
    _legacy_env_warning_emitted = True


def _warn_legacy_home() -> None:
    global _legacy_home_warning_emitted
    if _legacy_home_warning_emitted:
        return
    warnings.warn(
        "Recall is using an existing Remembrance data directory in place. "
        "No files were moved; set RECALL_HOME when you are ready to choose a new location.",
        RecallCompatibilityWarning,
        stacklevel=3,
    )
    _legacy_home_warning_emitted = True


def get_env(suffix: str, default: str | None = None) -> str | None:
    """Return RECALL_* first, then its deprecated REMEMBRANCE_* fallback."""
    canonical_name = f"RECALL_{suffix}"
    legacy_name = f"REMEMBRANCE_{suffix}"

    if canonical_name in os.environ:
        return os.environ[canonical_name]
    if legacy_name in os.environ:
        _warn_legacy_env(canonical_name, legacy_name)
        return os.environ[legacy_name]
    return default


def resolve_home(user_home: Path | None = None) -> Path:
    """Resolve Recall's data directory without hiding an existing legacy database."""
    explicit_recall = os.environ.get("RECALL_HOME")
    if explicit_recall:
        return Path(explicit_recall).expanduser()

    home = user_home if user_home is not None else Path.home()
    default_recall = home / ".recall"
    if default_recall.exists():
        return default_recall

    explicit_legacy = os.environ.get("REMEMBRANCE_HOME")
    if explicit_legacy:
        _warn_legacy_home()
        return Path(explicit_legacy).expanduser()

    default_legacy = home / ".remembrance"
    if default_legacy.exists():
        _warn_legacy_home()
        return default_legacy

    return default_recall


def _reset_warning_state() -> None:
    """Reset process warning guards for isolated compatibility tests."""
    global _legacy_env_warning_emitted, _legacy_home_warning_emitted
    _legacy_env_warning_emitted = False
    _legacy_home_warning_emitted = False
