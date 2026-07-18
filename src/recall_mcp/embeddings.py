"""Embedding providers used by semantic capture and retrieval."""

from __future__ import annotations

import hashlib
import json
import math
import struct
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol


class EmbeddingError(RuntimeError):
    """Raised when an embedding backend cannot return a safe vector."""


@dataclass(frozen=True)
class EmbeddingResult:
    vector: list[float]
    model: str
    content_hash: str

    @property
    def dimensions(self) -> int:
        return len(self.vector)

    def to_bytes(self) -> bytes:
        return struct.pack(f"{len(self.vector)}f", *self.vector)


class EmbeddingProvider(Protocol):
    model: str

    def embed(self, text: str) -> EmbeddingResult:
        """Return a normalized embedding for text."""


class OllamaEmbeddingProvider:
    """Generate embeddings with Ollama's /api/embed endpoint."""

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "embeddinggemma",
        timeout: float = 15.0,
        max_concurrency: int = 2,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._semaphore = threading.BoundedSemaphore(max_concurrency)

    def embed(self, text: str) -> EmbeddingResult:
        if not text or not text.strip():
            raise EmbeddingError("cannot embed empty text")

        payload = json.dumps(
            {"model": self.model, "input": text, "truncate": True}
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/api/embed",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with self._semaphore:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
        except (
            OSError,
            TimeoutError,
            urllib.error.URLError,
            json.JSONDecodeError,
        ) as exc:
            raise EmbeddingError(f"Ollama embedding failed: {exc}") from exc

        embeddings = body.get("embeddings")
        if not isinstance(embeddings, list) or not embeddings:
            raise EmbeddingError("Ollama returned no embeddings")
        vector = embeddings[0]
        if not isinstance(vector, list) or not vector:
            raise EmbeddingError("Ollama returned an invalid embedding")

        try:
            values = [float(value) for value in vector]
        except (TypeError, ValueError) as exc:
            raise EmbeddingError("Ollama returned a non-numeric embedding") from exc
        if not all(math.isfinite(value) for value in values):
            raise EmbeddingError("Ollama returned a non-finite embedding")

        norm = math.sqrt(sum(value * value for value in values))
        if norm == 0:
            raise EmbeddingError("Ollama returned a zero embedding")
        normalized = [value / norm for value in values]
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return EmbeddingResult(normalized, self.model, digest)
