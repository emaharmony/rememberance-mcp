"""Embedding package — provider-agnostic text embedding (Phase 1, isolated)."""

from remembrance_mcp.embed.embed import (  # noqa: F401
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
