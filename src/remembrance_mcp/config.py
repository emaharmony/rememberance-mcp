"""
Configuration — Environment Variables & Defaults

PATTERN: Environment-based Configuration (Twelve-Factor App)
=============================================================

This follows the "Twelve-Factor App" methodology:
  https://12factor.net/config

Key principle: Configuration should live in environment variables,
NOT in code. This makes the app:
  1. Portable — same code works on Mac, Linux, Docker, Cloud
  2. Testable — override config in tests without touching code
  3. Secure — secrets never checked into source control

We use a simple dataclass with os.environ reads instead of pydantic-settings
to keep dependencies minimal. If you want validation, you can swap in
pydantic-settings later (it's a drop-in replacement).

PATTERN: Lazy Singleton
  Settings are created once and cached. This avoids re-reading env vars
  on every function call. The `_instance` class variable holds the singleton.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    """All configuration in one place. Override via MEMORY_MCP_* env vars."""

    # ── Paths ──────────────────────────────────────────────
    BASE_DIR: Path = field(
        default_factory=lambda: Path(
            os.environ.get("REMEMBRANCE_HOME", str(Path.home() / ".remembrance"))
        )
    )
    DB_PATH: Path = field(default=None)
    GATE_MODEL_PATH: Path = field(default=None)

    # ── Gate Thresholds ────────────────────────────────────
    SKIP_THRESHOLD: float = 0.7
    COLD_THRESHOLD: float = 0.5
    ACTIVE_THRESHOLD: float = 0.5
    PERSIST_THRESHOLD: float = 0.7

    # ── Tier TTLs (seconds) ───────────────────────────────
    COLD_TTL: int = 86400  # 1 day
    ACTIVE_TTL: int = 30 * 86400  # 30 days
    PERSIST_TTL: int = -1  # -1 = never expires

    # ── Extraction ─────────────────────────────────────────
    EXTRACT_MODEL: str = "nemotron-3-nano:4b"
    OLLAMA_BASE_URL: str = "http://localhost:11434"

    # ── Search ─────────────────────────────────────────────
    SEARCH_MODEL: str = "all-MiniLM-L6-v2"
    SEARCH_RESULTS_LIMIT: int = 10

    # ── Server ─────────────────────────────────────────────
    MCP_SERVER_NAME: str = "remembrance"
    MCP_SERVER_VERSION: str = "0.1.0"

    # ── Singleton ──────────────────────────────────────────
    _instance = None

    def __post_init__(self):
        """Compute derived paths and create directories."""
        if self.DB_PATH is None:
            self.DB_PATH = self.BASE_DIR / "memory.db"
        if self.GATE_MODEL_PATH is None:
            self.GATE_MODEL_PATH = self.BASE_DIR / "models" / "distilbert-memory-gate"

        # Ensure directories exist
        self.BASE_DIR.mkdir(parents=True, exist_ok=True)
        (self.BASE_DIR / "models").mkdir(parents=True, exist_ok=True)

    @classmethod
    def get(cls) -> "Settings":
        """Get or create the singleton Settings instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
