"""Chunking package — split long memories into overlapping chunks (§5.4)."""

from remembrance_mcp.chunk.chunk import (  # noqa: F401
    CHARS_PER_TOKEN,
    DEFAULT_OVERLAP_TOKENS,
    DEFAULT_SINGLE_CHUNK_THRESHOLD_TOKENS,
    DEFAULT_TARGET_TOKENS,
    chunk_text,
)
