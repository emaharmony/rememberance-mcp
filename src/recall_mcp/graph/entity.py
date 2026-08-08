"""
Entity Detection — Extract Entities from Text (Zero-LLM)

PATTERN: Deterministic Entity Extraction (Regex + Lookup)
===========================================================

This module extracts entity references from text using ONLY regex
and entity registry lookups — no LLM calls. This is the "zero-cost
graph wiring" pattern from gbrain: every write extracts entities
and creates edges at near-zero cost.

WHY NOT LLM-BASED EXTRACTION?
- LLM extraction costs tokens and adds latency (~200ms-2s)
- Regex extraction is <1ms and deterministic
- For V1, regex catches 80%+ of entities (proper nouns, known names)
- LLM-based extraction can be added as an optional second pass later

EXTRACTION PIPELINE:
1. Regex scan for proper nouns, project names, decision patterns
2. Match candidates against entity registry (fuzzy match)
3. Classify unknown candidates by context (person vs project vs concept)
4. Return detected entities with confidence scores

EDGE TYPE INFERENCE:
Context patterns determine edge types:
- "Ema decided Prism..." → (ema, decided_about, prism)
- "Mango works on Prism..." → (mango, works_on, prism)
- "Prism uses NATS..." → (prism, depends_on, nats)
- "Prism stays domain-agnostic" → (prism, related_to, domain-agnostic)
"""

from __future__ import annotations
from typing import TypedDict

import re
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class DetectedEntity:
    """An entity detected in text."""

    name: str  # the name as it appears in text
    entity_type: str  # person, project, concept, tool, decision
    confidence: float  # 0.0 to 1.0
    edge_type: str  # mentions, decided_about, works_on, related_to, depends_on
    context: str  # surrounding text snippet for evidence


class KnownEntity(TypedDict):
    type: str
    aliases: list[str]


# ── Known Entity Patterns ──────────────────────────────────────

# These are hardcoded known entities for bootstrapping.
# Once the entity registry has data, detection uses the registry first.
KNOWN_ENTITIES: dict[str, KnownEntity] = {
    # People
    "ema": {"type": "person", "aliases": ["emmanuel", "rhem", "rhemma"]},
    "mango": {"type": "person", "aliases": []},
    "kirbii": {"type": "person", "aliases": []},
    "navii": {"type": "person", "aliases": []},
    "lumi": {"type": "person", "aliases": []},
    # Projects
    "prism": {"type": "project", "aliases": ["ai-hedge-prism"]},
    "recall": {
        "type": "project",
        # Former product spellings remain recognizable during migration.
        "aliases": ["recall-mcp", "remembrance", "remembrance-mcp", "memory-mcp"],
    },
    "eggventura": {"type": "project", "aliases": ["pet tycoon"]},
    "bassbook": {"type": "project", "aliases": []},
    "openclaw": {"type": "project", "aliases": []},
    # Concepts
    "dilbert": {"type": "concept", "aliases": ["distilbert", "dilbert-gate"]},
    "nemotron": {"type": "concept", "aliases": ["nemotron-3-nano"]},
    "nats": {"type": "tool", "aliases": []},
    "ollama": {"type": "tool", "aliases": []},
    "sqlite": {"type": "tool", "aliases": []},
}

# Decision patterns → edge type inference
DECISION_PATTERNS = [
    (r"\b(decided|decides|chose|confirmed|ruling)\b", "decided_about"),
    (r"\b(works?\s+on|implementing|building|shipping)\b", "works_on"),
    (r"\b(depends?\s+on|requires?|needs?|uses?)\b", "depends_on"),
    (r"\b(related\s+to|connected\s+to|linked\s+to)\b", "related_to"),
]

# Sentence boundary for context extraction
SENTENCE_RE = re.compile(r"[^.!?]+[.!?]+")


class EntityDetector:
    """
    Detect entities in text using regex + registry lookup.

    Usage:
        detector = EntityDetector(entity_store)
        entities = detector.detect("Ema decided Prism stays domain-agnostic")
        # → [DetectedEntity("ema", "person", 0.9, "decided_about"),
        #    DetectedEntity("prism", "project", 0.9, "decided_about")]
    """

    def __init__(self, entity_store=None):
        """
        Args:
            entity_store: EntityStore instance for registry lookups.
                         If None, uses KNOWN_ENTITIES only.
        """
        self.entity_store = entity_store

    def detect(self, text: str) -> list[DetectedEntity]:
        """
        Detect entities in a piece of text.

        Pipeline:
        1. Find proper nouns and known entity names
        2. Match against entity registry
        3. Infer edge types from context
        4. Return detected entities with confidence
        """
        detected = []
        seen = set()  # deduplicate by name

        # Step 1: Check known entities first (highest confidence)
        for name, info in KNOWN_ENTITIES.items():
            # Check main name
            if self._text_mentions(text, name) and name not in seen:
                seen.add(name)
                edge_type = self._infer_edge_type(text, name)
                context = self._extract_context(text, name)
                detected.append(
                    DetectedEntity(
                        name=name,
                        entity_type=info["type"],
                        confidence=0.9,
                        edge_type=edge_type,
                        context=context,
                    )
                )

            # Check aliases
            for alias in info.get("aliases", []):
                if self._text_mentions(text, alias) and name not in seen:
                    seen.add(name)
                    edge_type = self._infer_edge_type(text, alias)
                    context = self._extract_context(text, alias)
                    detected.append(
                        DetectedEntity(
                            name=name,  # canonical name, not alias
                            entity_type=info["type"],
                            confidence=0.85,
                            edge_type=edge_type,
                            context=context,
                        )
                    )

        # Step 2: Check entity registry (if available)
        if self.entity_store:
            # Look for registered entities not in KNOWN_ENTITIES
            all_entities = self.entity_store.list_entities(limit=200)
            for entity in all_entities:
                entity_name = entity["name"]
                entity_id = entity["id"]
                if entity_id in seen:
                    continue

                if self._text_mentions(text, entity_name):
                    seen.add(entity_id)
                    edge_type = self._infer_edge_type(text, entity_name)
                    context = self._extract_context(text, entity_name)
                    detected.append(
                        DetectedEntity(
                            name=entity_name,
                            entity_type=entity["type"],
                            confidence=0.85,
                            edge_type=edge_type,
                            context=context,
                        )
                    )

                # Check aliases from registry
                for alias in entity.get("aliases", []):
                    if entity_id in seen:
                        break
                    if self._text_mentions(text, alias):
                        seen.add(entity_id)
                        edge_type = self._infer_edge_type(text, alias)
                        context = self._extract_context(text, alias)
                        detected.append(
                            DetectedEntity(
                                name=entity_name,
                                entity_type=entity["type"],
                                confidence=0.8,
                                edge_type=edge_type,
                                context=context,
                            )
                        )

        # Step 3: Regex-based proper noun detection (lower confidence)
        # Capitalized words that aren't sentence starters
        proper_nouns = self._extract_proper_nouns(text)
        for noun in proper_nouns:
            noun_lower = noun.lower()
            if noun_lower in seen:
                continue
            # Skip common words and short words
            if len(noun) < 3 or noun_lower in {
                "the",
                "and",
                "but",
                "for",
                "not",
                "yes",
                "ok",
            }:
                continue

            seen.add(noun_lower)
            edge_type = self._infer_edge_type(text, noun)
            context = self._extract_context(text, noun)
            detected.append(
                DetectedEntity(
                    name=noun_lower,
                    entity_type=self._classify_unknown(noun, text),
                    confidence=0.5,
                    edge_type=edge_type,
                    context=context,
                )
            )

        return detected

    def _text_mentions(self, text: str, name: str) -> bool:
        """Check if text mentions a name (case-insensitive, word boundary)."""
        pattern = r"\b" + re.escape(name) + r"\b"
        return bool(re.search(pattern, text, re.IGNORECASE))

    def _infer_edge_type(self, text: str, entity_name: str) -> str:
        """
        Infer the edge type from surrounding context.

        Looks for patterns like "X decided Y", "X works on Y",
        "X depends on Y" in the sentence containing the entity mention.
        """
        # Find the sentence containing the entity
        sentences = SENTENCE_RE.findall(text)
        relevant = ""
        for s in sentences:
            if self._text_mentions(s, entity_name):
                relevant = s.lower()
                break

        if not relevant:
            relevant = text.lower()

        # Check decision patterns
        for pattern, edge_type in DECISION_PATTERNS:
            if re.search(pattern, relevant):
                return edge_type

        # Default: general mention
        return "mentions"

    def _extract_context(self, text: str, entity_name: str) -> str:
        """Extract a short context snippet around the entity mention."""
        match = re.search(r"\b" + re.escape(entity_name) + r"\b", text, re.IGNORECASE)
        if not match:
            return text[:200]

        start = max(0, match.start() - 50)
        end = min(len(text), match.end() + 50)
        snippet = text[start:end].strip()
        if start > 0:
            snippet = "..." + snippet
        if end < len(text):
            snippet += "..."
        return snippet

    def _extract_proper_nouns(self, text: str) -> list[str]:
        """
        Extract proper nouns (capitalized words not at sentence start).

        Heuristic: a word starting with uppercase that is NOT
        the first word of a sentence is likely a proper noun.
        """
        # Split into sentences
        sentences = re.split(r"(?<=[.!?])\s+", text)
        nouns = []

        for sentence in sentences:
            words = sentence.split()
            for i, word in enumerate(words):
                # Skip first word of sentence (always capitalized)
                if i == 0:
                    continue
                # Check if word starts with uppercase
                clean = word.strip(".,;:!?\"')")
                if clean and clean[0].isupper() and len(clean) > 2:
                    if clean.lower() not in {
                        "the",
                        "and",
                        "but",
                        "for",
                        "this",
                        "that",
                        "with",
                        "from",
                    }:
                        nouns.append(clean)

        return nouns

    def _classify_unknown(self, name: str, text: str) -> str:
        """
        Classify an unknown entity by context.

        Heuristics:
        - If text mentions "project" → project
        - If text mentions "person", "he", "she" → person
        - If text mentions "decided", "decision" → decision
        - If text mentions "concept", "pattern", "framework" → concept
        - Default: concept
        """
        text_lower = text.lower()

        if any(w in text_lower for w in ["project", "repo", "repository", "codebase"]):
            return "project"
        if any(w in text_lower for w in ["person", "he", "she", "they", "him", "her"]):
            return "person"
        if any(w in text_lower for w in ["decided", "decision", "chose", "ruling"]):
            return "decision"
        if any(
            w in text_lower for w in ["concept", "pattern", "framework", "architecture"]
        ):
            return "concept"
        if any(w in text_lower for w in ["tool", "library", "service", "database"]):
            return "tool"

        return "concept"  # safe default
