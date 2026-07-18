"""Safe logging configuration for the Recall service."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit


class JsonFormatter(logging.Formatter):
    """Render one machine-readable JSON object per log record."""

    def format(self, record: logging.LogRecord) -> str:
        event: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            event["exception"] = self.formatException(record.exc_info)
        return json.dumps(event, ensure_ascii=True)


def configure_logging(*, level: int, json_logs: bool) -> None:
    """Configure the process root logger exactly once at service startup."""

    handler = logging.StreamHandler()
    if json_logs:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
    logging.basicConfig(level=level, handlers=[handler], force=True)


def safe_endpoint(value: str) -> str:
    """Remove URL credentials before an endpoint is written to logs."""

    try:
        parsed = urlsplit(value)
    except ValueError:
        return "<invalid endpoint>"
    if not parsed.netloc:
        return value
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    port = f":{parsed.port}" if parsed.port is not None else ""
    return urlunsplit((parsed.scheme, hostname + port, parsed.path, "", ""))
