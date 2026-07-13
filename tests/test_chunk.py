"""Phase 4.1 — chunking primitive (`chunk_text`, §5.4).

Deterministic pure-function tests: single-chunk collapse, overlap, full coverage,
and configurability via args + env.
"""

from remembrance_mcp.chunk.chunk import CHARS_PER_TOKEN, chunk_text


def test_empty_returns_no_chunks():
    assert chunk_text("") == []
    assert chunk_text("   \n  ") == []


def test_short_text_is_single_chunk():
    text = "a short memory well under the threshold"
    assert chunk_text(text) == [text]


def test_short_text_is_stripped_single_chunk():
    assert chunk_text("  padded memory  ") == ["padded memory"]


def _make_words(n):
    # distinct 4-char words so we can track coverage/order precisely
    return " ".join(f"w{i:03d}" for i in range(n))


def test_long_text_splits_into_multiple_chunks():
    # target 10 tokens (~40 chars), threshold 10 tokens → 60 words must split.
    text = _make_words(60)
    chunks = chunk_text(text, target_tokens=10, overlap_tokens=2, single_chunk_threshold_tokens=10)
    assert len(chunks) > 1


def test_chunks_cover_all_words_in_order():
    text = _make_words(60)
    chunks = chunk_text(text, target_tokens=10, overlap_tokens=3, single_chunk_threshold_tokens=10)
    # Every original word appears in at least one chunk...
    all_words = text.split()
    seen = set()
    for c in chunks:
        seen.update(c.split())
    assert set(all_words) <= seen
    # ...and first/last words are preserved at the boundaries.
    assert chunks[0].split()[0] == "w000"
    assert chunks[-1].split()[-1] == "w059"


def test_consecutive_chunks_overlap():
    text = _make_words(60)
    chunks = chunk_text(text, target_tokens=10, overlap_tokens=3, single_chunk_threshold_tokens=10)
    # The start of chunk k+1 must reappear from the tail of chunk k (overlap).
    for a, b in zip(chunks, chunks[1:]):
        a_words = a.split()
        b_first = b.split()[0]
        assert b_first in a_words, f"no overlap between consecutive chunks: {b_first!r}"


def test_chunks_respect_target_char_budget():
    text = _make_words(80)
    target = 8
    chunks = chunk_text(
        text, target_tokens=target, overlap_tokens=2, single_chunk_threshold_tokens=8
    )
    budget = target * CHARS_PER_TOKEN
    # Each chunk fits the budget (words are short, so no single-word overflow here).
    assert all(len(c) <= budget for c in chunks)


def test_smaller_target_yields_more_chunks():
    text = _make_words(100)
    few = chunk_text(text, target_tokens=20, overlap_tokens=2, single_chunk_threshold_tokens=20)
    many = chunk_text(text, target_tokens=5, overlap_tokens=1, single_chunk_threshold_tokens=5)
    assert len(many) > len(few)


def test_overlap_clamped_below_target_still_progresses():
    # Degenerate config (overlap >= target) must not loop forever; it is clamped.
    text = _make_words(40)
    chunks = chunk_text(text, target_tokens=5, overlap_tokens=999, single_chunk_threshold_tokens=5)
    assert len(chunks) >= 1
    assert chunks[-1].split()[-1] == "w039"


def test_single_long_word_is_its_own_chunk():
    long_word = "x" * 200
    text = f"{long_word} and some more words here to force splitting behavior overall"
    chunks = chunk_text(text, target_tokens=5, overlap_tokens=1, single_chunk_threshold_tokens=5)
    assert any(long_word in c for c in chunks)


def test_env_overrides_defaults(monkeypatch):
    text = _make_words(60)
    # Without override, this is short enough (default threshold 512 tokens) → 1 chunk.
    assert len(chunk_text(text)) == 1
    # Force small sizing via env → multiple chunks.
    monkeypatch.setenv("REMEMBRANCE_CHUNK_TARGET_TOKENS", "8")
    monkeypatch.setenv("REMEMBRANCE_CHUNK_OVERLAP_TOKENS", "2")
    monkeypatch.setenv("REMEMBRANCE_CHUNK_SINGLE_THRESHOLD_TOKENS", "8")
    assert len(chunk_text(text)) > 1


def test_explicit_args_win_over_env(monkeypatch):
    monkeypatch.setenv("REMEMBRANCE_CHUNK_SINGLE_THRESHOLD_TOKENS", "1")
    text = _make_words(60)
    # Explicit high threshold overrides the tiny env threshold → single chunk.
    assert len(chunk_text(text, single_chunk_threshold_tokens=10_000)) == 1
