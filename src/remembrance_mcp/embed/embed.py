"""
Embedding backends — pluggable, provider-agnostic text embedding.

Mirrors the gate-backend pattern (`gate/backends.py` + `gate/registry.py`):
an ABC, concrete provider backends, and an ordered fallback chain selected by
the ``REMEMBRANCE_EMBED_BACKENDS`` env var.

Design invariants (see docs/semantic-retrieval.md §5.2–5.3):
  - No hardcoded dimension. ``embed_text`` returns ``(bytes, dim, model_id)`` and
    the dimension is *discovered* from the produced vector, so swapping models
    never forces a migration.
  - Vectors are serialized as packed little-endian float32 (``struct`` "f"),
    the same format ``search/hybrid.py`` reads back — so these bytes are ready
    for the Phase 3 vector-search wiring without reformatting.

FALLBACK CAVEAT: ``HashEmbedBackend`` produces correctly-shaped, deterministic
vectors so the pipeline *runs* offline (and in tests) with zero dependencies,
but it has **no semantic quality**. Genuine retrieval needs Ollama or OpenAI.
It is a correctness scaffold, not a quality one.

This module is intentionally isolated (Phase 1): it wires into nothing. The
capture/query/backfill call sites are added in Phase 3.
"""

import hashlib
import json
import logging
import math
import os
import struct
import urllib.request
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

# Dimension of the dependency-free hash backend. Real backends discover their
# own dimension from the model output; this is only the offline scaffold's size.
DEFAULT_HASH_DIM = 256

# Default model names per provider (overridable via REMEMBRANCE_EMBED_MODEL).
DEFAULT_OLLAMA_MODEL = "nomic-embed-text"
DEFAULT_OPENAI_MODEL = "text-embedding-3-small"
DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


# ── Serialization helpers ────────────────────────────────────────────────────
# Kept identical to search/hybrid.py's _vector_to_bytes / _bytes_to_vector so a
# vector embedded here round-trips through the search layer unchanged.


def vector_to_bytes(vec: list[float]) -> bytes:
    """Pack a list of floats into little-endian float32 bytes."""
    return struct.pack(f"{len(vec)}f", *vec)


def bytes_to_vector(data: bytes) -> list[float]:
    """Unpack float32 bytes back into a list of floats."""
    count = len(data) // 4
    if count == 0:
        return []
    return list(struct.unpack(f"{count}f", data))


# ── Backend interface ────────────────────────────────────────────────────────


class BaseEmbedBackend(ABC):
    """Abstract embedding backend.

    Contract:
      - ``embed(text)`` returns the embedding as packed float32 bytes.
      - ``model_id`` is a stable identifier stored per row so search only ever
        compares vectors produced by the *same* model.
      - ``dim`` reports the last observed embedding dimension (0 until the first
        successful ``embed`` for remote models that discover it at call time).
    """

    _dim: int = 0

    @property
    @abstractmethod
    def model_id(self) -> str:
        """Stable identifier of the embedding model, e.g. ``ollama:nomic-embed-text``."""

    @property
    def dim(self) -> int:
        return self._dim

    @abstractmethod
    def embed(self, text: str) -> bytes:
        """Embed ``text`` and return packed little-endian float32 bytes."""

    def is_available(self) -> bool:
        """Cheap liveness probe; the chain still catches embed() failures."""
        return True


# ── Dependency-free offline backend ──────────────────────────────────────────


class HashEmbedBackend(BaseEmbedBackend):
    """Deterministic, dependency-free embeddings for offline/test correctness.

    Expands SHA-256 digests of the text into a fixed-dimension, L2-normalized
    vector. Same text → identical vector; different text → different vector.
    NO semantic meaning — a correctness scaffold only (see module caveat).
    """

    def __init__(self, dim: int = DEFAULT_HASH_DIM):
        if dim <= 0:
            raise ValueError("hash embedding dimension must be positive")
        self._target_dim = dim
        self._dim = dim

    @property
    def model_id(self) -> str:
        return f"hash-{self._target_dim}"

    def embed(self, text: str) -> bytes:
        return vector_to_bytes(self._hash_vector(text, self._target_dim))

    @staticmethod
    def _hash_vector(text: str, dim: int) -> list[float]:
        data = text.encode("utf-8")
        out: list[float] = []
        counter = 0
        while len(out) < dim:
            digest = hashlib.sha256(data + counter.to_bytes(4, "big")).digest()
            for b in digest:
                out.append((b / 127.5) - 1.0)  # map byte 0..255 → ~[-1, 1)
                if len(out) >= dim:
                    break
            counter += 1
        norm = math.sqrt(sum(x * x for x in out)) or 1.0
        return [x / norm for x in out]


# ── Remote provider backends ─────────────────────────────────────────────────


class OllamaEmbedBackend(BaseEmbedBackend):
    """Embeddings via a local Ollama server (``POST /api/embeddings``)."""

    def __init__(
        self,
        model: str = DEFAULT_OLLAMA_MODEL,
        base_url: str = DEFAULT_OLLAMA_HOST,
        timeout: int = 30,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @property
    def model_id(self) -> str:
        return f"ollama:{self.model}"

    def embed(self, text: str) -> bytes:
        payload = json.dumps({"model": self.model, "prompt": text}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/api/embeddings",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        vec = result.get("embedding")
        if not vec:
            raise ValueError(f"Ollama returned no embedding for model {self.model}")
        self._dim = len(vec)
        return vector_to_bytes([float(x) for x in vec])

    def is_available(self) -> bool:
        try:
            req = urllib.request.Request(f"{self.base_url}/api/tags")
            with urllib.request.urlopen(req, timeout=3):
                return True
        except Exception:
            return False


class OpenAIEmbedBackend(BaseEmbedBackend):
    """Embeddings via the OpenAI ``/embeddings`` endpoint (text-embedding-3-*)."""

    def __init__(
        self,
        api_key: str = "",
        model: str = DEFAULT_OPENAI_MODEL,
        base_url: str = DEFAULT_OPENAI_BASE_URL,
        timeout: int = 30,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @property
    def model_id(self) -> str:
        return f"openai:{self.model}"

    def embed(self, text: str) -> bytes:
        if not self.api_key:
            raise RuntimeError("OpenAIEmbedBackend requires an API key")
        payload = json.dumps({"model": self.model, "input": text}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        vec = result["data"][0]["embedding"]
        if not vec:
            raise ValueError(f"OpenAI returned no embedding for model {self.model}")
        self._dim = len(vec)
        return vector_to_bytes([float(x) for x in vec])

    def is_available(self) -> bool:
        return bool(self.api_key)


# ── Fallback chain ───────────────────────────────────────────────────────────


class EmbedFallbackChain:
    """Ordered embedding backends; first success wins.

    Public surface: ``embed_text(text) -> (bytes, dim, model_id)``. The dimension
    is derived from the produced vector, never hardcoded.
    """

    def __init__(self, backends: list[BaseEmbedBackend]):
        if not backends:
            raise ValueError("EmbedFallbackChain requires at least one backend")
        self.backends = backends

    def embed_text(self, text: str) -> tuple[bytes, int, str]:
        errors: list[str] = []
        for backend in self.backends:
            try:
                data = backend.embed(text)
                dim = len(data) // 4
                if dim == 0:
                    raise ValueError("produced an empty vector")
                return data, dim, backend.model_id
            except Exception as e:  # noqa: BLE001 - try the next backend, report if all fail
                logger.warning("Embed backend '%s' failed: %s, trying next", backend.model_id, e)
                errors.append(f"{backend.model_id}: {e}")
        raise RuntimeError(f"All embed backends failed: {errors}")


# ── Builder ──────────────────────────────────────────────────────────────────

_KNOWN_BACKENDS = ("hash", "ollama", "openai")


def build_embed_chain(backend_names: list[str] | None = None) -> EmbedFallbackChain:
    """Build an ``EmbedFallbackChain`` from names or ``REMEMBRANCE_EMBED_BACKENDS``.

    Defaults to ``["hash"]`` (offline scaffold). ``hash`` is always appended as
    the ultimate fallback so embedding never hard-fails the pipeline.

    Env vars (see §5.3):
      - ``REMEMBRANCE_EMBED_BACKENDS``  e.g. ``"ollama,hash"`` / ``"openai,hash"``
      - ``REMEMBRANCE_EMBED_MODEL``     provider model override
      - ``REMEMBRANCE_EMBED_HOST``      Ollama host (falls back to ``OLLAMA_BASE_URL``)
      - ``OPENAI_API_KEY``              required for the openai backend
    """
    if backend_names is None:
        env = os.environ.get("REMEMBRANCE_EMBED_BACKENDS", "")
        backend_names = [b.strip() for b in env.split(",") if b.strip()] or ["hash"]

    model = os.environ.get("REMEMBRANCE_EMBED_MODEL", "")
    host = os.environ.get("REMEMBRANCE_EMBED_HOST") or os.environ.get(
        "OLLAMA_BASE_URL", DEFAULT_OLLAMA_HOST
    )
    api_key = os.environ.get("OPENAI_API_KEY", "")

    backends: list[BaseEmbedBackend] = []
    for name in backend_names:
        n = name.lower()
        if n == "hash":
            backends.append(HashEmbedBackend())
        elif n == "ollama":
            backends.append(OllamaEmbedBackend(model=model or DEFAULT_OLLAMA_MODEL, base_url=host))
        elif n == "openai":
            if api_key:
                backends.append(
                    OpenAIEmbedBackend(api_key=api_key, model=model or DEFAULT_OPENAI_MODEL)
                )
            else:
                logger.warning("OpenAI embed backend skipped: no OPENAI_API_KEY")
        else:
            logger.warning("Unknown embed backend '%s', skipping. Known: %s", n, _KNOWN_BACKENDS)

    if not any(isinstance(b, HashEmbedBackend) for b in backends):
        backends.append(HashEmbedBackend())

    return EmbedFallbackChain(backends)


_default_chain: EmbedFallbackChain | None = None


def embed_text(text: str) -> tuple[bytes, int, str]:
    """Embed ``text`` via the process-wide, env-configured chain.

    Returns ``(vector_bytes, dim, model_id)``. The chain is built once and cached.
    """
    global _default_chain
    if _default_chain is None:
        _default_chain = build_embed_chain()
    return _default_chain.embed_text(text)
