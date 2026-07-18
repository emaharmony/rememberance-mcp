"""REST authentication, rate limiting, and lightweight metrics."""

from __future__ import annotations

import hmac
import ipaddress
import os
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from recall_mcp.config import Settings


def is_loopback_address(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


class TokenAuthenticator:
    """Validate current and recently rotated bearer tokens."""

    def __init__(self, settings: Settings):
        self.settings = settings

    @staticmethod
    def _read_token(path: Path | None) -> str | None:
        if path is None:
            return None
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return token or None

    def configured(self) -> bool:
        return self.settings.auth_configured

    def validate(self, authorization: str | None) -> bool:
        if not self.configured():
            return True
        if not authorization or not authorization.startswith("Bearer "):
            return False
        supplied = authorization[7:].strip()
        candidates = [
            self.settings.API_TOKEN,
            self._read_token(self.settings.API_TOKEN_FILE),
        ]

        previous_file = self.settings.PREVIOUS_API_TOKEN_FILE
        if previous_file is None and self.settings.API_TOKEN_FILE is not None:
            current = self.settings.API_TOKEN_FILE
            previous_file = current.with_name(current.name + ".previous")
        if previous_file is not None:
            try:
                age = time.time() - previous_file.stat().st_mtime
            except OSError:
                age = float("inf")
            if age <= 24 * 3600:
                candidates.append(self._read_token(previous_file))

        return any(
            candidate is not None
            and hmac.compare_digest(supplied.encode(), candidate.encode())
            for candidate in candidates
        )


class SlidingWindowRateLimiter:
    def __init__(self, reads_per_minute: int, writes_per_minute: int):
        self.limits = {"read": reads_per_minute, "write": writes_per_minute}
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, client: str, kind: str) -> bool:
        now = time.monotonic()
        cutoff = now - 60.0
        key = (client, kind)
        with self._lock:
            events = self._events[key]
            while events and events[0] < cutoff:
                events.popleft()
            if len(events) >= self.limits[kind]:
                return False
            events.append(now)
            return True


class ServiceMetrics:
    """Thread-safe Prometheus text metrics without an external dependency."""

    def __init__(self):
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], int] = (
            defaultdict(int)
        )
        self._gauges: dict[str, float] = {}
        self._lock = threading.Lock()

    def increment(self, name: str, **labels: str) -> None:
        key = (name, tuple(sorted((key, str(value)) for key, value in labels.items())))
        with self._lock:
            self._counters[key] += 1

    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = value

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            counters = list(self._counters.items())
            gauges = list(self._gauges.items())
        for (name, labels), counter_value in sorted(counters):
            suffix = ""
            if labels:
                rendered = ",".join(f'{key}="{value}"' for key, value in labels)
                suffix = f"{{{rendered}}}"
            lines.append(f"{name}{suffix} {counter_value}")
        for name, gauge_value in sorted(gauges):
            lines.append(f"{name} {gauge_value}")
        return "\n".join(lines) + "\n"


def verify_token_file_permissions(path: Path | None) -> bool:
    """Best-effort secret permission check used by readiness and doctor."""
    if path is None or not path.exists():
        return False
    if os.name == "nt":
        return True
    return path.stat().st_mode & 0o077 == 0
