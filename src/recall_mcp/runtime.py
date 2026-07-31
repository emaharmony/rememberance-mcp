"""Environment-backed production settings for Recall."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from pathlib import Path

from recall_mcp.compat import get_env, resolve_home


def _text(name: str, default: str) -> str:
    return str(get_env(name, default))


def _optional_text(name: str) -> str | None:
    value = get_env(name)
    return value.strip() if value and value.strip() else None


def _integer(name: str, default: int) -> int:
    value = get_env(name)
    return default if value is None else int(value)


def _number(name: str, default: float) -> float:
    value = get_env(name)
    return default if value is None else float(value)


def _boolean(name: str, default: bool) -> bool:
    value = get_env(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"RECALL_{name} must be a boolean")


def _path(name: str) -> Path | None:
    value = get_env(name)
    return Path(value).expanduser() if value else None


def _csv(name: str) -> tuple[str, ...]:
    value = get_env(name, "") or ""
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass
class Settings:
    """Typed settings whose defaults are read from RECALL_* variables."""

    BASE_DIR: Path = field(default_factory=lambda: _path("HOME"))  # type: ignore[assignment]
    DB_PATH: Path = field(default_factory=lambda: _path("DB_PATH"))  # type: ignore[assignment]
    GATE_MODEL_PATH: Path = field(  # type: ignore[assignment]
        default_factory=lambda: _path("GATE_MODEL_PATH")
    )

    SKIP_THRESHOLD: float = field(
        default_factory=lambda: _number("SKIP_THRESHOLD", 0.7)
    )
    COLD_THRESHOLD: float = field(
        default_factory=lambda: _number("COLD_THRESHOLD", 0.5)
    )
    ACTIVE_THRESHOLD: float = field(
        default_factory=lambda: _number("ACTIVE_THRESHOLD", 0.5)
    )
    PERSIST_THRESHOLD: float = field(
        default_factory=lambda: _number("PERSIST_THRESHOLD", 0.7)
    )

    COLD_TTL: int = field(default_factory=lambda: _integer("COLD_TTL", 86400))
    ACTIVE_TTL: int = field(default_factory=lambda: _integer("ACTIVE_TTL", 30 * 86400))
    PERSIST_TTL: int = field(default_factory=lambda: _integer("PERSIST_TTL", -1))

    EXTRACT_MODEL: str = field(
        default_factory=lambda: _text("EXTRACT_MODEL", "nemotron-3-nano:4b")
    )
    EMBED_MODEL: str = field(
        default_factory=lambda: _text("EMBED_MODEL", "embeddinggemma")
    )
    OLLAMA_BASE_URL: str = field(
        default_factory=lambda: _text("OLLAMA_URL", "http://127.0.0.1:11434")
    )
    OLLAMA_TIMEOUT_SECONDS: float = field(
        default_factory=lambda: _number("OLLAMA_TIMEOUT_SECONDS", 15.0)
    )
    OLLAMA_MAX_CONCURRENCY: int = field(
        default_factory=lambda: _integer("OLLAMA_MAX_CONCURRENCY", 2)
    )
    EMBEDDINGS_ENABLED: bool = field(
        default_factory=lambda: _boolean("EMBEDDINGS_ENABLED", False)
    )
    SEARCH_MODEL: str = field(
        default_factory=lambda: _text("SEARCH_MODEL", "embeddinggemma")
    )
    SEARCH_RESULTS_LIMIT: int = field(
        default_factory=lambda: _integer("SEARCH_RESULTS_LIMIT", 10)
    )

    HOST: str = field(default_factory=lambda: _text("HOST", "127.0.0.1"))
    PORT: int = field(default_factory=lambda: _integer("PORT", 8788))
    API_TOKEN: str | None = field(default_factory=lambda: _optional_text("API_TOKEN"))
    API_TOKEN_FILE: Path | None = field(default_factory=lambda: _path("API_TOKEN_FILE"))
    PREVIOUS_API_TOKEN_FILE: Path | None = field(
        default_factory=lambda: _path("PREVIOUS_API_TOKEN_FILE")
    )
    ALLOWED_ORIGINS: tuple[str, ...] = field(
        default_factory=lambda: _csv("ALLOWED_ORIGINS")
    )
    MAX_BODY_BYTES: int = field(
        default_factory=lambda: _integer("MAX_BODY_BYTES", 1_048_576)
    )
    MAX_CAPTURE_CHARS: int = field(
        default_factory=lambda: _integer("MAX_CAPTURE_CHARS", 262_144)
    )
    MAX_RESULTS: int = field(default_factory=lambda: _integer("MAX_RESULTS", 100))
    MAX_GRAPH_DEPTH: int = field(default_factory=lambda: _integer("MAX_GRAPH_DEPTH", 4))
    READS_PER_MINUTE: int = field(
        default_factory=lambda: _integer("READS_PER_MINUTE", 120)
    )
    WRITES_PER_MINUTE: int = field(
        default_factory=lambda: _integer("WRITES_PER_MINUTE", 30)
    )
    CAPTURE_PROCESSING_TIMEOUT: float = field(
        default_factory=lambda: _number("CAPTURE_PROCESSING_TIMEOUT", 15.0)
    )
    PROCESSING_QUEUE_LIMIT: int = field(
        default_factory=lambda: _integer("PROCESSING_QUEUE_LIMIT", 64)
    )
    OUTBOX_POLL_INTERVAL: float = field(
        default_factory=lambda: _number("OUTBOX_POLL_INTERVAL", 0.25)
    )
    OUTBOX_LEASE_SECONDS: float = field(
        default_factory=lambda: _number("OUTBOX_LEASE_SECONDS", 300.0)
    )
    OUTBOX_MAX_ATTEMPTS: int = field(
        default_factory=lambda: _integer("OUTBOX_MAX_ATTEMPTS", 10)
    )
    OUTBOX_RETRY_BASE_SECONDS: float = field(
        default_factory=lambda: _number("OUTBOX_RETRY_BASE_SECONDS", 2.0)
    )

    NATS_URL: str = field(
        default_factory=lambda: _text("NATS_URL", "nats://127.0.0.1:4222")
    )
    NATS_CREDS_FILE: Path | None = field(
        default_factory=lambda: _path("NATS_CREDS_FILE")
    )
    NATS_SUBJECT: str = field(
        default_factory=lambda: _text("NATS_SUBJECT", "*.agent.output")
    )
    NATS_STREAM: str = field(
        default_factory=lambda: _text("NATS_STREAM", "RECALL_AGENT_OUTPUT")
    )
    NATS_CONSUMER: str = field(
        default_factory=lambda: _text("NATS_CONSUMER", "recall-capture-v1")
    )
    NATS_DLQ_SUBJECT: str = field(
        default_factory=lambda: _text("NATS_DLQ_SUBJECT", "recall.agent.output.dlq")
    )

    MCP_SERVER_NAME: str = field(
        default_factory=lambda: _text("MCP_SERVER_NAME", "recall")
    )
    MCP_SERVER_VERSION: str = field(
        default_factory=lambda: _text("MCP_SERVER_VERSION", "2.1.0")
    )
    JSON_LOGS: bool = field(default_factory=lambda: _boolean("JSON_LOGS", False))

    _instance = None

    def __post_init__(self) -> None:
        if self.BASE_DIR is None:
            self.BASE_DIR = self.DB_PATH.parent if self.DB_PATH else resolve_home()
        if self.DB_PATH is None:
            self.DB_PATH = self.BASE_DIR / "memory.db"
        if self.GATE_MODEL_PATH is None:
            self.GATE_MODEL_PATH = self.BASE_DIR / "models" / "distilbert-memory-gate"

        self.BASE_DIR = Path(self.BASE_DIR)
        self.DB_PATH = Path(self.DB_PATH)
        self.GATE_MODEL_PATH = Path(self.GATE_MODEL_PATH)

        if not 1 <= self.PORT <= 65535:
            raise ValueError("RECALL_PORT must be between 1 and 65535")
        if self.MAX_BODY_BYTES <= 0 or self.MAX_CAPTURE_CHARS <= 0:
            raise ValueError("request and capture limits must be positive")
        if self.MAX_RESULTS <= 0 or self.MAX_GRAPH_DEPTH < 1:
            raise ValueError("search and graph limits must be positive")
        if self.OLLAMA_MAX_CONCURRENCY < 1:
            raise ValueError("RECALL_OLLAMA_MAX_CONCURRENCY must be positive")
        if self.READS_PER_MINUTE <= 0 or self.WRITES_PER_MINUTE <= 0:
            raise ValueError("request rate limits must be positive")
        if self.CAPTURE_PROCESSING_TIMEOUT <= 0:
            raise ValueError("RECALL_CAPTURE_PROCESSING_TIMEOUT must be positive")
        if self.PROCESSING_QUEUE_LIMIT < self.OLLAMA_MAX_CONCURRENCY:
            raise ValueError("processing queue limit must cover worker concurrency")
        if self.OUTBOX_POLL_INTERVAL <= 0 or self.OUTBOX_LEASE_SECONDS <= 0:
            raise ValueError("outbox polling and lease durations must be positive")
        if self.OUTBOX_MAX_ATTEMPTS < 1 or self.OUTBOX_RETRY_BASE_SECONDS <= 0:
            raise ValueError("outbox retry settings must be positive")
        if self.OLLAMA_TIMEOUT_SECONDS <= 0:
            raise ValueError("RECALL_OLLAMA_TIMEOUT_SECONDS must be positive")
        if not self.OLLAMA_BASE_URL.startswith(("http://", "https://")):
            raise ValueError("RECALL_OLLAMA_URL must be an HTTP(S) URL")
        if not self.NATS_URL.startswith(("nats://", "tls://")):
            raise ValueError("RECALL_NATS_URL must be a NATS URL")
        if not _is_loopback(self.HOST) and not (self.API_TOKEN or self.API_TOKEN_FILE):
            raise ValueError(
                "Refusing a non-loopback RECALL_HOST without RECALL_API_TOKEN_FILE"
            )

        self.BASE_DIR.mkdir(parents=True, exist_ok=True)
        (self.BASE_DIR / "models").mkdir(parents=True, exist_ok=True)

    @property
    def auth_configured(self) -> bool:
        return bool(self.API_TOKEN or self.API_TOKEN_FILE)

    @classmethod
    def get(cls) -> "Settings":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
