"""Offline unit tests for the embedding module (Phase 1).

Everything here runs via HashEmbedBackend — deterministic and dependency-free,
so no Ollama/OpenAI server is needed. The remote backends are exercised only for
their pure/offline behavior (model_id, missing-key guard, chain fallback).
"""

import struct

import pytest

from remembrance_mcp.embed.embed import (
    DEFAULT_HASH_DIM,
    BaseEmbedBackend,
    EmbedFallbackChain,
    HashEmbedBackend,
    OllamaEmbedBackend,
    OpenAIEmbedBackend,
    build_embed_chain,
    bytes_to_vector,
    embed_text,
    vector_to_bytes,
)

# ── Serialization round-trip ─────────────────────────────────────────────────


def test_vector_bytes_roundtrip():
    vec = [0.0, 1.5, -2.25, 3.125]
    data = vector_to_bytes(vec)
    assert len(data) == len(vec) * 4  # float32
    back = bytes_to_vector(data)
    assert back == pytest.approx(vec)


def test_bytes_to_vector_empty():
    assert bytes_to_vector(b"") == []


# ── HashEmbedBackend ─────────────────────────────────────────────────────────


def test_hash_deterministic_same_text():
    b = HashEmbedBackend()
    assert b.embed("the shared memory brain") == b.embed("the shared memory brain")


def test_hash_differs_by_text():
    b = HashEmbedBackend()
    assert b.embed("alpha") != b.embed("beta")


def test_hash_dim_and_model_id():
    b = HashEmbedBackend()
    data = b.embed("anything")
    assert b.dim == DEFAULT_HASH_DIM
    assert len(data) // 4 == DEFAULT_HASH_DIM
    assert b.model_id == f"hash-{DEFAULT_HASH_DIM}"


def test_hash_custom_dim():
    b = HashEmbedBackend(dim=64)
    data = b.embed("x")
    assert len(data) // 4 == 64
    assert b.model_id == "hash-64"


def test_hash_is_l2_normalized():
    vec = bytes_to_vector(HashEmbedBackend().embed("normalize me"))
    norm = sum(x * x for x in vec) ** 0.5
    assert norm == pytest.approx(1.0, abs=1e-5)


def test_hash_rejects_nonpositive_dim():
    with pytest.raises(ValueError):
        HashEmbedBackend(dim=0)


# ── EmbedFallbackChain ───────────────────────────────────────────────────────


def test_chain_embed_text_returns_triple():
    chain = EmbedFallbackChain([HashEmbedBackend(dim=32)])
    data, dim, model_id = chain.embed_text("hello")
    assert isinstance(data, bytes)
    assert dim == 32
    assert len(data) // 4 == dim
    assert model_id == "hash-32"


def test_chain_requires_a_backend():
    with pytest.raises(ValueError):
        EmbedFallbackChain([])


class _AlwaysFails(BaseEmbedBackend):
    @property
    def model_id(self) -> str:
        return "boom"

    def embed(self, text: str) -> bytes:
        raise RuntimeError("backend down")


def test_chain_first_success_wins():
    # Failing backend is skipped; the next (hash) succeeds.
    chain = EmbedFallbackChain([_AlwaysFails(), HashEmbedBackend(dim=16)])
    data, dim, model_id = chain.embed_text("resilient")
    assert dim == 16
    assert model_id == "hash-16"


def test_chain_all_fail_raises():
    chain = EmbedFallbackChain([_AlwaysFails(), _AlwaysFails()])
    with pytest.raises(RuntimeError):
        chain.embed_text("nope")


# ── build_embed_chain / env config ───────────────────────────────────────────


def test_build_default_is_hash(monkeypatch):
    monkeypatch.delenv("REMEMBRANCE_EMBED_BACKENDS", raising=False)
    chain = build_embed_chain()
    assert isinstance(chain.backends[-1], HashEmbedBackend)
    data, dim, model_id = chain.embed_text("x")
    assert model_id.startswith("hash-")


def test_build_appends_hash_fallback(monkeypatch):
    # Even an all-remote request keeps hash as the ultimate offline fallback.
    monkeypatch.setenv("REMEMBRANCE_EMBED_BACKENDS", "ollama")
    monkeypatch.setenv("REMEMBRANCE_EMBED_HOST", "http://127.0.0.1:9")  # unused offline
    chain = build_embed_chain()
    assert any(isinstance(b, HashEmbedBackend) for b in chain.backends)
    assert isinstance(chain.backends[0], OllamaEmbedBackend)


def test_build_unknown_backend_skipped(monkeypatch):
    monkeypatch.setenv("REMEMBRANCE_EMBED_BACKENDS", "does-not-exist")
    chain = build_embed_chain()
    # Only the hash fallback remains.
    assert len(chain.backends) == 1
    assert isinstance(chain.backends[0], HashEmbedBackend)


def test_build_openai_skipped_without_key(monkeypatch):
    monkeypatch.setenv("REMEMBRANCE_EMBED_BACKENDS", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    chain = build_embed_chain()
    assert all(not isinstance(b, OpenAIEmbedBackend) for b in chain.backends)
    assert isinstance(chain.backends[-1], HashEmbedBackend)


def test_build_explicit_names_override_env(monkeypatch):
    monkeypatch.setenv("REMEMBRANCE_EMBED_BACKENDS", "ollama")
    chain = build_embed_chain(["hash"])
    assert len(chain.backends) == 1
    assert isinstance(chain.backends[0], HashEmbedBackend)


# ── Remote backends: offline-checkable behavior ──────────────────────────────


def test_ollama_model_id():
    assert OllamaEmbedBackend(model="nomic-embed-text").model_id == "ollama:nomic-embed-text"


def test_openai_model_id():
    assert OpenAIEmbedBackend(model="text-embedding-3-small").model_id == (
        "openai:text-embedding-3-small"
    )


def test_openai_requires_key():
    with pytest.raises(RuntimeError):
        OpenAIEmbedBackend(api_key="").embed("x")


def test_openai_is_available_reflects_key():
    assert OpenAIEmbedBackend(api_key="sk-test").is_available() is True
    assert OpenAIEmbedBackend(api_key="").is_available() is False


# ── Module-level convenience ─────────────────────────────────────────────────


def test_module_embed_text(monkeypatch):
    monkeypatch.delenv("REMEMBRANCE_EMBED_BACKENDS", raising=False)
    import remembrance_mcp.embed.embed as embed_mod

    embed_mod._default_chain = None  # reset cache so env change takes effect
    data, dim, model_id = embed_text("hello world")
    assert isinstance(data, bytes)
    assert dim > 0
    assert struct.calcsize(f"{dim}f") == len(data)
    assert model_id.startswith("hash-")
