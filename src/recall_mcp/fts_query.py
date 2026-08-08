"""Shared SQLite FTS5 MATCH-expression construction.

FTS5's default bareword grammar ANDs whitespace-separated tokens, so a long
natural-language query (e.g. a task description) only matches a document
containing every single token verbatim. Stripping common stopwords and
joining the rest with OR turns that into "match any meaningful token",
which BM25 ranking then naturally favors documents matching more of.

Used by both `search/hybrid.py` (the hot search path) and
`store/memory.py` (`MemoryStoreV2.search_fts`) so the two don't drift.
"""

import re
from typing import Optional

STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at", "for",
    "is", "are", "was", "were", "be", "been", "being", "with", "as", "by",
    "that", "this", "it", "its", "from", "we", "i", "you", "he", "she",
    "they", "do", "does", "did", "what", "which", "who", "whom", "about",
    "into", "over", "after", "before", "between", "not", "no", "so", "if",
    "than", "then", "there", "here", "when", "where", "how", "our", "your",
    "their", "up", "out", "just",
}
MAX_TOKENS = 32


def build_fts_match(query: str) -> Optional[str]:
    """Build an OR-joined, quoted FTS5 MATCH expression from a query string.

    Returns None when no usable tokens can be extracted (caller should
    fall back to LIKE search instead of issuing an empty MATCH).
    """
    tokens = re.findall(r"[A-Za-z0-9][\w.-]*", query)
    tokens = [t for t in tokens if len(t) > 1]
    if not tokens:
        return None

    filtered = [t for t in tokens if t.lower() not in STOPWORDS]
    use = filtered if filtered else tokens
    use = use[:MAX_TOKENS]

    # Quote each token as an FTS5 string literal: this neutralizes FTS5
    # special characters (*, :, parens, trailing punctuation) and turns
    # bareword implicit-AND into an explicit OR across terms. The token
    # regex above never produces an embedded '"', but escape defensively.
    double_quote = '"'
    terms = [
        double_quote + t.replace(double_quote, double_quote * 2) + double_quote
        for t in use
    ]
    return " OR ".join(terms)
