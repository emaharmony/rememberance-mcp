"""
Ollama Gate Backend — LLM-based classification fallback when DilBERT is unavailable

PATTERN: API-based Classification (Graceful Degradation)
=========================================================

When DilBERT is not available (model not downloaded, Python environment
missing transformers), the gate falls back to Ollama for classification.

This backend uses a simple prompt-based approach:
1. Send the text + classification instructions to Ollama
2. Parse the response for SKIP/COLD/ACTIVE/PERSIST
3. Return GateResult with confidence

WHY OLLAMA AS FALLBACK?
- Zero setup cost if Ollama is already running (which it is for extraction)
- Uses any model available — defaults to Nemotron-3-nano (fast + cheap)
- Consistent with our "zero-cost local" philosophy

TRADEOFF: Slower than DilBERT (~500ms vs ~50ms) but more flexible.
Any model can be registered for gate classification in the future.
"""

from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error

from recall_mcp.gate.gate import GateDecision, GateResult
from recall_mcp.gate_backends import BaseGateBackend

logger = logging.getLogger(__name__)

# ── Classification Prompt ──────────────────────────────────────

GATE_PROMPT = """Classify this text for memory importance. Return ONLY one word: SKIP, COLD, ACTIVE, or PERSIST.

SKIP: Trivial, no value storing (greetings, acks, small talk)
COLD: Low importance, short-term only (casual mentions, minor details)
ACTIVE: Worth remembering, current relevance (decisions, tasks, progress)
PERSIST: Critical, must remember long-term (key decisions, architecture, identity)

Text: {text}

Classification:"""


class OllamaGateBackend(BaseGateBackend):
    """
    Ollama-based gate classification backend.

    Uses any Ollama model to classify text into SKIP/COLD/ACTIVE/PERSIST.
    Default model: nemotron-3-nano:4b (fast, local, zero-cost).
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "nemotron-3-nano:4b",
        timeout: int = 10,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    @property
    def name(self) -> str:
        return f"ollama:{self.model}"

    def classify(self, text: str) -> GateResult:
        """
        Classify text using Ollama.

        Returns GateResult with decision and confidence.
        Falls back to HEURISTIC classification if Ollama is unavailable.
        """
        # Try Ollama classification
        try:
            prompt = GATE_PROMPT.format(text=text[:500])
            payload = json.dumps(
                {
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {
                        "temperature": 0.1,  # Low temp for deterministic classification
                        "num_predict": 10,  # Only need one word
                    },
                }
            ).encode("utf-8")

            req = urllib.request.Request(
                f"{self.base_url}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
            )

            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                response_text = result.get("response", "").strip().upper()

            # Parse the response
            decision = self._parse_decision(response_text)
            confidence = (
                0.75  # LLM classification confidence (lower than DilBERT's 0.929)
            )

            return GateResult(
                decision=decision,
                confidence=confidence,
            )

        except Exception as e:
            logger.warning(f"Ollama gate failed: {e}. Using heuristic fallback.")
            # Fallback to heuristic
            return self._heuristic_fallback(text)

    def _parse_decision(self, response: str) -> GateDecision:
        """Parse the LLM response into a GateDecision."""
        response = response.strip().upper()

        # Direct match
        for decision in GateDecision:
            if decision.value.upper() in response:
                return decision

        # Partial match (in case model outputs "ACTIVE PERSIST" or similar)
        if "PERSIST" in response:
            return GateDecision.PERSIST
        if "ACTIVE" in response:
            return GateDecision.ACTIVE
        if "COLD" in response:
            return GateDecision.COLD

        # Default: COLD (conservative — don't skip, but don't persist)
        return GateDecision.COLD

    def _heuristic_fallback(self, text: str) -> GateResult:
        """
        Simple rule-based fallback when Ollama is unavailable.

        Heuristics:
        - Empty/short text → SKIP
        - Contains decision keywords → PERSIST
        - Contains task/progress keywords → ACTIVE
        - Everything else → COLD
        """
        text_lower = text.lower().strip()

        if len(text_lower) < 5:
            return GateResult(
                decision=GateDecision.SKIP,
                confidence=0.9,
            )

        # Decision keywords
        decision_words = {
            "decided",
            "decision",
            "chose",
            "confirmed",
            "ruling",
            "defined",
            "architected",
        }
        if any(w in text_lower for w in decision_words):
            return GateResult(
                decision=GateDecision.PERSIST,
                confidence=0.7,
            )

        # Active keywords
        active_words = {
            "implementing",
            "working on",
            "building",
            "shipping",
            "fixing",
            "testing",
            "deploy",
        }
        if any(w in text_lower for w in active_words):
            return GateResult(
                decision=GateDecision.ACTIVE,
                confidence=0.7,
            )

        # Skip patterns
        skip_patterns = {
            "ok",
            "thanks",
            "got it",
            "sure",
            "yep",
            "cool",
            "nice",
            "lol",
            "haha",
        }
        if text_lower in skip_patterns or len(text_lower) < 15:
            return GateResult(
                decision=GateDecision.SKIP,
                confidence=0.8,
            )

        return GateResult(
            decision=GateDecision.COLD,
            confidence=0.5,
        )

    def is_available(self) -> bool:
        """Check if Ollama is running and the model is available."""
        try:
            req = urllib.request.Request(
                f"{self.base_url}/api/tags",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                models = [m["name"] for m in data.get("models", [])]
                # Check if our model (or a prefix match) is available
                for m in models:
                    if self.model in m or m in self.model:
                        return True
                logger.warning(f"Model {self.model} not found. Available: {models}")
                return False
        except Exception:
            return False
