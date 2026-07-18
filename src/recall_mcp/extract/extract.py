"""
Memory Extractor — Structured Data Extraction

PATTERN: Structured Output from LLMs
=======================================

This layer takes raw text and extracts structured data:
  - summary: what was said (concise)
  - category: project, person, preference, decision, task, strategy, session
  - tier: how important (cold, active, persist)
  - key_topics: searchable tags

EXTRACTION vs CLASSIFICATION:
  The gate (Layer 1) is a CLASSIFIER — it puts text into buckets.
  The extractor (Layer 2) is a GENERATIVE model — it creates structured output.

  Classification: "This is ACTIVE" → fast, cheap, narrow
  Extraction: "This is about SelfQuest, PR consolidation, Ema" → slow, expensive, rich

WHY NEMOTRON?
  Nemotron-3-nano is NVIDIA's small model (~2.5GB), optimized for:
  - Structured output (JSON, not free text)
  - Low latency on consumer hardware
  - Good instruction following

  It runs locally via Ollama — no API key, no cloud dependency.
  For production, you could swap this for GPT-4o-mini or Claude Haiku.

DESIGN PATTERN: Strategy Pattern
  The extractor uses the Strategy pattern — we define an interface
  (extract) and can swap implementations (Nemotron, OpenAI, Claude)
  without changing the pipeline code.
"""

import json
import logging
from dataclasses import dataclass
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


@dataclass
class ExtractionResult:
    """Structured output from the extractor."""

    summary: str
    category: str  # project, person, preference, decision, task, strategy, session
    tier: str  # cold, active, persist
    key_topics: list[str]


class BaseExtractor(ABC):
    """Interface for extraction strategies. Swap implementations without changing pipeline."""

    @abstractmethod
    def extract(
        self, text: str, source: str = "", gate_decision: str = ""
    ) -> ExtractionResult:
        """Extract structured data from raw text."""
        ...


class OllamaExtractor(BaseExtractor):
    """
    Extract using a local model via Ollama.

    Ollama is a local model server that runs on your machine.
    It exposes an HTTP API that's compatible with OpenAI's format.
    Models are downloaded once and served from a local cache.

    WHY OLLAMA?
    - No API key needed
    - Runs offline
    - Same API for all models (swap model name, nothing else changes)
    - Production-ready (used by companies for internal AI tools)
    """

    def __init__(
        self, model: str = "nemotron-3-nano", base_url: str = "http://localhost:11434"
    ):
        self.model = model
        self.base_url = base_url

    def extract(
        self, text: str, source: str = "", gate_decision: str = ""
    ) -> ExtractionResult:
        """
        Send text to Ollama for structured extraction.

        PROMPT ENGINEERING NOTE:
        We use a very specific prompt format because:
        1. It tells the model EXACTLY what JSON schema to output
        2. It provides examples (few-shot prompting)
        3. It constrains the output space (only valid categories/tiers)

        This is called "prompt engineering" and it's a core AI engineering skill.
        The prompt IS the product for many AI applications.
        """
        prompt = f"""Extract structured memory data from this text. Output ONLY valid JSON.

Categories: project, person, preference, decision, task, strategy, session
Tiers: cold (low importance, 1-day TTL), active (current context, 30-day TTL), persist (important forever)

Text: {text}
Source: {source}
Gate classification: {gate_decision}

Output JSON:
{{
  "summary": "concise summary of what this text contains",
  "category": "one of: project, person, preference, decision, task, strategy, session",
  "tier": "one of: cold, active, persist",
  "key_topics": ["topic1", "topic2"]
}}"""

        try:
            import urllib.request
            import urllib.error

            payload = json.dumps(
                {
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                    # Reasoning models (e.g. nemotron-3-nano) otherwise emit their
                    # output in a separate `thinking` field and leave `response`
                    # empty, which fails JSON parsing. Disabling thinking makes the
                    # JSON land in `response`. Ignored by non-reasoning models.
                    "think": False,
                }
            ).encode("utf-8")

            req = urllib.request.Request(
                f"{self.base_url}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
            )

            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))

            response_text = result.get("response", "")

            # Parse JSON from model output
            # The model might wrap JSON in markdown code blocks — strip those
            response_text = response_text.strip()
            if response_text.startswith("```"):
                response_text = response_text.split("```")[1]
                if response_text.startswith("json"):
                    response_text = response_text[4:]

            data = json.loads(response_text)

            return ExtractionResult(
                summary=data.get("summary", text[:200]),
                category=self._validate_category(data.get("category", "project")),
                tier=self._validate_tier(data.get("tier", "active")),
                key_topics=data.get("key_topics", []),
            )

        except (
            json.JSONDecodeError,
            urllib.error.URLError,
            KeyError,
            TimeoutError,
            OSError,
        ) as e:
            # EXTRACTION FAILURE PATTERN:
            # When extraction fails, we DON'T lose the data.
            # We fall back to storing raw text with sensible defaults.
            # This is called "graceful degradation" — the system continues
            # to function even when a component fails.
            logger.warning(f"Extraction failed, using defaults: {e}")
            return ExtractionResult(
                summary=text[:200],
                category="project",
                tier=gate_decision.lower()
                if gate_decision in ("COLD", "ACTIVE", "PERSIST")
                else "active",
                key_topics=[],
            )

    @staticmethod
    def _validate_category(cat: str) -> str:
        """Enforce valid categories — prevents model hallucination."""
        valid = {
            "project",
            "person",
            "preference",
            "decision",
            "task",
            "strategy",
            "session",
        }
        return cat.lower() if cat.lower() in valid else "project"

    @staticmethod
    def _validate_tier(tier: str) -> str:
        """Enforce valid tiers — prevents model hallucination."""
        valid = {"cold", "active", "persist"}
        return tier.lower() if tier.lower() in valid else "active"


class StubExtractor(BaseExtractor):
    """
    Fallback extractor for when no model is available.

    DESIGN PATTERN: Null Object Pattern
    Instead of returning None or raising an error, we return a valid
    object with default values. This keeps the pipeline flowing.
    """

    def extract(
        self, text: str, source: str = "", gate_decision: str = ""
    ) -> ExtractionResult:
        return ExtractionResult(
            summary=text[:200],
            category="project",
            tier=gate_decision.lower()
            if gate_decision in ("COLD", "ACTIVE", "PERSIST")
            else "active",
            key_topics=[],
        )
