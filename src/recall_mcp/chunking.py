"""
Chunking — split long memories into overlapping chunks (semantic-retrieval.md §5.4).

One vector per long memory averages everything into mush. Chunking lets small
notes and long documents coexist accurately: a long memory is split into
several overlapping chunks that are each embedded and searched independently
(see `search/hybrid.py::search_chunks_with_embedding`); a short memory
collapses to a single chunk so the write/search path stays uniform.

SIZING DEFAULTS ARE PROVISIONAL (see docs/semantic-retrieval.md §9 "Open
questions"). These are conventional starting points, are **env-overridable**
(nothing hardcoded into behavior), and are flagged for review:
  - RECALL_CHUNK_TARGET_TOKENS            (default 512)
  - RECALL_CHUNK_OVERLAP_TOKENS           (default 64)
  - RECALL_CHUNK_SINGLE_THRESHOLD_TOKENS  (default 512)

(Legacy `REMEMBRANCE_CHUNK_*` names are also honored via `recall_mcp.compat`,
matching every other Recall environment variable.)

Tokens are approximated as ~4 characters/token to avoid adding a tokenizer
dependency, the same heuristic Recall already uses elsewhere for token
estimates.
"""

from __future__ import annotations

from recall_mcp.compat import get_env

# ~4 chars/token heuristic.
CHARS_PER_TOKEN = 4

DEFAULT_TARGET_TOKENS = 512
DEFAULT_OVERLAP_TOKENS = 64
DEFAULT_SINGLE_CHUNK_THRESHOLD_TOKENS = 512


def _int_env(name: str, default: int) -> int:
    raw = get_env(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def chunk_text(
    text: str,
    target_tokens: int | None = None,
    overlap_tokens: int | None = None,
    single_chunk_threshold_tokens: int | None = None,
) -> list[str]:
    """Split ``text`` into overlapping chunks.

    - Empty/whitespace-only -> ``[]``.
    - Content at/below the single-chunk threshold -> ``[text]`` (one chunk).
    - Otherwise -> a list of overlapping chunks, each approximately
      ``target_tokens`` long (except when a single word exceeds the target),
      consecutive chunks sharing approximately ``overlap_tokens`` of text.
      Every word appears in at least one chunk and word order is preserved.

    Explicit args win over env vars, which win over the module defaults.
    """
    text = (text or "").strip()
    if not text:
        return []

    target = (
        target_tokens
        if target_tokens is not None
        else _int_env("CHUNK_TARGET_TOKENS", DEFAULT_TARGET_TOKENS)
    )
    overlap = (
        overlap_tokens
        if overlap_tokens is not None
        else _int_env("CHUNK_OVERLAP_TOKENS", DEFAULT_OVERLAP_TOKENS)
    )
    threshold = (
        single_chunk_threshold_tokens
        if single_chunk_threshold_tokens is not None
        else _int_env(
            "CHUNK_SINGLE_THRESHOLD_TOKENS",
            DEFAULT_SINGLE_CHUNK_THRESHOLD_TOKENS,
        )
    )

    # Sanitize: target >= 1; overlap in [0, target-1] so the window always advances.
    target = max(1, target)
    overlap = max(0, min(overlap, target - 1))

    target_chars = target * CHARS_PER_TOKEN
    threshold_chars = threshold * CHARS_PER_TOKEN
    overlap_chars = overlap * CHARS_PER_TOKEN

    if len(text) <= threshold_chars:
        return [text]

    words = text.split()
    chunks: list[str] = []
    n = len(words)
    i = 0
    while i < n:
        # Greedily accumulate words up to the char budget (always >= 1 word).
        cur: list[str] = []
        cur_len = 0
        j = i
        while j < n:
            wlen = len(words[j]) + (1 if cur else 0)
            if cur and cur_len + wlen > target_chars:
                break
            cur.append(words[j])
            cur_len += wlen
            j += 1
        chunks.append(" ".join(cur))
        if j >= n:
            break

        # Step back so the next chunk re-includes ~overlap_chars of trailing words.
        back_len = 0
        back = 0
        k = j - 1
        while k > i and back_len < overlap_chars:
            back_len += len(words[k]) + 1
            back += 1
            k -= 1
        next_i = j - back
        if next_i <= i:  # guarantee forward progress
            next_i = i + 1
        i = next_i

    return chunks
