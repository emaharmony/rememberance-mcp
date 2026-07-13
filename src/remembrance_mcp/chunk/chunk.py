"""
Chunking — split long memories into overlapping chunks (semantic-retrieval.md §5.4).

One vector per long memory averages everything into mush; chunking lets small
notes and long documents coexist accurately. Short memories collapse to a single
chunk so the write/search path is uniform.

SIZING DEFAULTS ARE PROVISIONAL. The roadmap ("Open decisions carried forward")
defers chunk size / overlap / single-chunk threshold to a human. These defaults
are conventional starting points, are **env-overridable** (nothing hardcoded into
behavior), and are flagged for review — they are NOT a final product decision:
  - REMEMBRANCE_CHUNK_TARGET_TOKENS         (default 512)
  - REMEMBRANCE_CHUNK_OVERLAP_TOKENS        (default 64)
  - REMEMBRANCE_CHUNK_SINGLE_THRESHOLD_TOKENS (default 512)

Tokens are approximated as ~4 characters (the same heuristic api/rest.py already
uses for its token estimate) to avoid adding a tokenizer dependency.
"""

import os

# ~4 chars/token heuristic (matches the token estimate in api/rest.py).
CHARS_PER_TOKEN = 4

DEFAULT_TARGET_TOKENS = 512
DEFAULT_OVERLAP_TOKENS = 64
DEFAULT_SINGLE_CHUNK_THRESHOLD_TOKENS = 512


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
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

    - Empty/whitespace-only → ``[]``.
    - Content at/below the single-chunk threshold → ``[text]`` (one chunk).
    - Otherwise → a list of overlapping chunks, each ≈ ``target_tokens`` long
      (except when a single word exceeds the target), consecutive chunks sharing
      ≈ ``overlap_tokens`` of text. Every word appears in at least one chunk and
      word order is preserved.

    Explicit args win over env vars, which win over the module defaults.
    """
    text = (text or "").strip()
    if not text:
        return []

    target = (
        target_tokens
        if target_tokens is not None
        else _int_env("REMEMBRANCE_CHUNK_TARGET_TOKENS", DEFAULT_TARGET_TOKENS)
    )
    overlap = (
        overlap_tokens
        if overlap_tokens is not None
        else _int_env("REMEMBRANCE_CHUNK_OVERLAP_TOKENS", DEFAULT_OVERLAP_TOKENS)
    )
    threshold = (
        single_chunk_threshold_tokens
        if single_chunk_threshold_tokens is not None
        else _int_env(
            "REMEMBRANCE_CHUNK_SINGLE_THRESHOLD_TOKENS",
            DEFAULT_SINGLE_CHUNK_THRESHOLD_TOKENS,
        )
    )

    # Sanitize: target ≥ 1; overlap in [0, target-1] so the window always advances.
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
        # Greedily accumulate words up to the char budget (always ≥ 1 word).
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

        # Step back so the next chunk re-includes ≈ overlap_chars of trailing words.
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
