"""Extract package — Structured data extraction from raw text."""

from recall_mcp.extract.extract import (
    BaseExtractor,
    OllamaExtractor,
    StubExtractor,
    ExtractionResult,
)  # noqa: F401

__all__ = [
    "BaseExtractor",
    "OllamaExtractor",
    "StubExtractor",
    "ExtractionResult",
]
