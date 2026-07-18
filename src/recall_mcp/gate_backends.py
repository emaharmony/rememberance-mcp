"""
Gate Backends — Pluggable Classification Strategy

PATTERN: Strategy Pattern (again, same as extraction)
=====================================================

The gate should work everywhere — Mac, Linux, cloud, no GPU, no local model.
We achieve this with a fallback chain:

  1. DilBERT (local, fast, 256MB) — if available
  2. OpenAI API (cloud, fast, $0.01/1K tokens) — if API key provided
  3. Heuristic (zero ML, rule-based, instant) — always available

The fallback chain means Recall MCP works on EVERY machine out of the box.
You install it, it works. If you have a GPU or want better accuracy, you opt in.

EFFECTIVENESS MONITORING:
========================
Every classification is logged with:
  - Which backend made the decision
  - Confidence score
  - The input text (truncated)
  - Timestamp

This lets you answer questions like:
  - Is DilBERT better than heuristics for our data? (compare accuracy)
  - What % of messages get SKIPPED? (filter efficiency)
  - Is the heuristic fallback good enough? (coverage vs quality)
  - Which categories are most common? (distribution analysis)

Metrics are stored in the same SQLite DB as memories, in a `gate_metrics` table.
"""

import json
import time
import logging
import re
import sqlite3
from abc import ABC, abstractmethod
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Optional

from recall_mcp.gate import GateDecision, GateResult

logger = logging.getLogger(__name__)


# ── Metrics ──────────────────────────────────────────────────────────────────


@dataclass
class GateMetric:
    """A single gate classification event, stored for effectiveness analysis."""

    timestamp: float
    backend: str  # "dilbert", "openai", "heuristic"
    text_preview: str  # first 100 chars of input
    decision: str  # SKIP, COLD, ACTIVE, PERSIST
    confidence: float  # 0.0 to 1.0
    fallback_used: bool  # did we fall back from a preferred backend?


class GateMetrics:
    """
    Stores gate classification events for effectiveness monitoring.

    This is the "observe" part of "observe, orient, decide, act".
    You can't improve what you don't measure.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Create metrics table if it doesn't exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS gate_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    backend TEXT NOT NULL,
                    text_preview TEXT,
                    decision TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    fallback_used INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_metrics_backend ON gate_metrics(backend)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_metrics_decision ON gate_metrics(decision)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_metrics_timestamp ON gate_metrics(timestamp)"
            )

    def record(self, metric: GateMetric):
        """Record a classification event."""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                """
                INSERT INTO gate_metrics (timestamp, backend, text_preview, decision, confidence, fallback_used)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (
                    metric.timestamp,
                    metric.backend,
                    metric.text_preview[:100],
                    metric.decision,
                    metric.confidence,
                    1 if metric.fallback_used else 0,
                ),
            )

    def summary(self, hours: int = 24) -> dict:
        """
        Get effectiveness summary for the last N hours.

        Returns:
            dict with keys: total, by_backend, by_decision, avg_confidence,
            fallback_rate, skip_rate
        """
        cutoff = time.time() - (hours * 3600)
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row

            total = conn.execute(
                "SELECT COUNT(*) as cnt FROM gate_metrics WHERE timestamp > ?",
                (cutoff,),
            ).fetchone()["cnt"]

            by_backend_rows = conn.execute(
                "SELECT backend, COUNT(*) as cnt, AVG(confidence) as avg_conf FROM gate_metrics WHERE timestamp > ? GROUP BY backend",
                (cutoff,),
            ).fetchall()
            by_backend = {
                row["backend"]: {
                    "count": row["cnt"],
                    "avg_confidence": round(row["avg_conf"], 3),
                }
                for row in by_backend_rows
            }

            by_decision = dict(
                conn.execute(
                    "SELECT decision, COUNT(*) as cnt FROM gate_metrics WHERE timestamp > ? GROUP BY decision",
                    (cutoff,),
                ).fetchall()
            )

            fallback_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM gate_metrics WHERE timestamp > ? AND fallback_used = 1",
                (cutoff,),
            ).fetchone()["cnt"]

            skip_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM gate_metrics WHERE timestamp > ? AND decision = 'SKIP'",
                (cutoff,),
            ).fetchone()["cnt"]

            avg_conf = (
                conn.execute(
                    "SELECT AVG(confidence) as avg FROM gate_metrics WHERE timestamp > ?",
                    (cutoff,),
                ).fetchone()["avg"]
                or 0.0
            )

        return {
            "period_hours": hours,
            "total_classifications": total,
            "by_backend": by_backend,
            "by_decision": dict(by_decision),
            "avg_confidence": round(avg_conf, 3),
            "fallback_rate": round(fallback_count / max(total, 1), 3),
            "skip_rate": round(skip_count / max(total, 1), 3),
        }


# ── Backend Interface ────────────────────────────────────────────────────────


class BaseGateBackend(ABC):
    """Interface for gate classification backends."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Backend identifier for metrics."""
        ...

    @abstractmethod
    def classify(self, text: str) -> GateResult:
        """Classify text into a gate decision."""
        ...


# ── Heuristic Backend (always available, zero dependencies) ────────────────────


class HeuristicBackend(BaseGateBackend):
    """
    Rule-based classifier that requires NO model, NO GPU, NO API key.

    ACCURACY: ~70-80% on typical conversation data
    SPEED: <1ms per classification
    COST: Free forever

    HOW IT WORKS:
      Uses hand-crafted rules based on text features:
      - Length (short messages are more likely SKIP)
      - Keywords (project terms → ACTIVE, decision terms → PERSIST)
      - Sentence structure (questions → COLD, declarations → ACTIVE/PERSIST)
      - Punctuation patterns (emojis/short replies → SKIP)

    WHEN TO USE:
      - First install before any model is downloaded
      - Environments without GPU or API access
      - As a baseline to compare ML models against

    This is your "it just works" option. Every Recall MCP install
    has this from day one, no configuration needed.
    """

    @property
    def name(self) -> str:
        return "heuristic"

    # Patterns for each classification
    SKIP_PATTERNS = [
        r"^(ok|okay|k|got it|gotcha|sure|yep|yup|yeah|nope|nah|hmm|hm|lol|ha|thx|thanks|ty|👍|😊|🙌|✅|❤️|🔥|💪|💯|🎉|😉|👋)$",
        r"^(yes|no|maybe|right|correct|wrong|true|false)$",
        r"^.{1,5}$",  # very short messages (1-5 chars)
    ]

    COLD_PATTERNS = [
        r"\?$",  # ends with question mark
        r"^(what|how|when|where|why|who|is|can|do|does|did|will|would|should|could)\b",
        r"^(hmm|interesting|oh|ah|well)\b",
    ]

    ACTIVE_PATTERNS = [
        r"\b(fix|implement| build|create|update|change|refactor|debug|deploy|test|merge|push|branch|commit|pr|issue|task|ticket)\b",
        r"\b(todo|blocker|progress|status|review|approve|request)\b",
        r"\b(error|bug|crash|fail|broken|issue|problem)\b",
    ]

    PERSIST_PATTERNS = [
        r"\b(decision|decided|architecture|design pattern|agreed|rule|policy|preference|always|never)\b",
        r"\b(name is|i am|i prefer|i want|my name|remember this|important|critical|must|don\'t forget)\b",
        r"\b(project|milestone|deadline|launch|release|version)\b",
    ]

    def classify(self, text: str) -> GateResult:
        text_lower = text.lower().strip()

        # Check SKIP patterns first (most common, cheapest to check)
        for pattern in self.SKIP_PATTERNS:
            if re.match(pattern, text_lower):
                return GateResult(decision=GateDecision.SKIP, confidence=0.85)

        # Check PERSIST patterns (highest value)
        for pattern in self.PERSIST_PATTERNS:
            if re.search(pattern, text_lower):
                return GateResult(decision=GateDecision.PERSIST, confidence=0.75)

        # Check ACTIVE patterns
        for pattern in self.ACTIVE_PATTERNS:
            if re.search(pattern, text_lower):
                return GateResult(decision=GateDecision.ACTIVE, confidence=0.70)

        # Check COLD patterns (questions, casual)
        for pattern in self.COLD_PATTERNS:
            if re.search(pattern, text_lower):
                return GateResult(decision=GateDecision.COLD, confidence=0.65)

        # Default: length-based heuristic
        if len(text) < 20:
            return GateResult(decision=GateDecision.SKIP, confidence=0.60)
        elif len(text) < 50:
            return GateResult(decision=GateDecision.COLD, confidence=0.55)
        else:
            return GateResult(decision=GateDecision.ACTIVE, confidence=0.60)


# ── OpenAI Backend ────────────────────────────────────────────────────────────


class OpenAIBackend(BaseGateBackend):
    """
    Cloud-based classification using OpenAI's API.

    ACCURACY: ~95%+ on conversation data
    SPEED: ~200-500ms per classification (network latency)
    COST: ~$0.01 per 1K classifications (gpt-4o-mini)

    HOW IT WORKS:
      Sends the text to OpenAI with a structured prompt asking for
      SKIP/COLD/ACTIVE/PERSIST classification. The model returns a
      JSON response with decision and confidence.

    WHEN TO USE:
      - Cloud deployments where local ML isn't available
      - As a comparison baseline against DilBERT
      - When you need the highest possible accuracy

    SETUP:
      Set OPENAI_API_KEY environment variable.
    """

    @property
    def name(self) -> str:
        return "openai"

    def __init__(self, api_key: str = "", model: str = "gpt-4o-mini"):
        self.api_key = api_key
        self.model = model

    def classify(self, text: str) -> GateResult:
        import urllib.request
        import os

        api_key = self.api_key or os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise ValueError("OPENAI_API_KEY not set")

        prompt = f"""Classify this text for memory relevance. Return ONLY JSON: {{"decision": "SKIP|COLD|ACTIVE|PERSIST", "confidence": 0.0-1.0}}

SKIP: Greetings, acknowledgments, small talk (don't store)
COLD: Questions, casual mentions (store briefly, 1-day TTL)
ACTIVE: Project state, tasks, current work (store 30 days)
PERSIST: Decisions, preferences, important facts, architecture (store forever)

Text: {text}"""

        payload = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {
                        "role": "system",
                        "content": "You classify text for memory relevance. Return only JSON.",
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                "max_tokens": 50,
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                content = result["choices"][0]["message"]["content"]
                # Parse JSON from model response
                data = json.loads(content)
                decision_str = data.get("decision", "ACTIVE").upper()
                confidence = float(data.get("confidence", 0.5))
                decision = (
                    GateDecision(decision_str.lower())
                    if decision_str.lower() in [d.value.lower() for d in GateDecision]
                    else GateDecision.ACTIVE
                )
                return GateResult(decision=decision, confidence=confidence)
        except Exception as e:
            logger.warning(f"OpenAI gate failed: {e}")
            raise


# ── DilBERT Backend (local ML) ──────────────────────────────────────────────


class DilBERTBackend(BaseGateBackend):
    """
    Local ML classification using fine-tuned DistilBERT.

    ACCURACY: ~90%+ on conversation data
    SPEED: <100ms per classification (CPU)
    COST: Free (runs locally)

    This is the same gate we've been using, just wrapped in the
    pluggable backend interface.
    """

    @property
    def name(self) -> str:
        return "dilbert"

    def __init__(self, model_path: Path, skip_threshold: float = 0.7):
        self.model_path = model_path
        self.skip_threshold = skip_threshold
        self._model: Any = None
        self._tokenizer: Any = None

    def _load_model(self):
        if self._model is not None:
            return
        from transformers import (
            DistilBertTokenizer,
            DistilBertForSequenceClassification,
        )

        if not self.model_path.exists():
            raise FileNotFoundError(f"DilBERT model not found at {self.model_path}")

        logger.info(f"Loading DilBERT gate model from {self.model_path}")
        self._tokenizer = DistilBertTokenizer.from_pretrained(str(self.model_path))
        self._model = DistilBertForSequenceClassification.from_pretrained(
            str(self.model_path)
        )
        self._model.eval()

    def classify(self, text: str) -> GateResult:
        import torch

        self._load_model()

        inputs = self._tokenizer(
            text, return_tensors="pt", truncation=True, max_length=512
        )
        with torch.no_grad():
            logits = self._model(**inputs).logits
        probs = torch.softmax(logits, dim=-1)[0]
        pred_class = int(torch.argmax(probs).item())
        confidence = probs[pred_class].item()

        labels = [
            GateDecision.SKIP,
            GateDecision.COLD,
            GateDecision.ACTIVE,
            GateDecision.PERSIST,
        ]
        decision = labels[pred_class]

        # Threshold check: if not confident in SKIP, promote to next best
        if decision == GateDecision.SKIP and confidence < self.skip_threshold:
            probs_no_skip = probs.clone()
            probs_no_skip[0] = 0
            next_best = int(torch.argmax(probs_no_skip).item())
            decision = labels[next_best]
            confidence = probs[next_best].item()

        return GateResult(decision=decision, confidence=confidence)


# ── Fallback Chain ───────────────────────────────────────────────────────────


class GateFallbackChain:
    """
    Chains multiple backends in priority order with fallback.

    PATTERN: Chain of Responsibility (again)
    ============================================
    Try each backend in order. If the first fails, try the next.
    This gives us:
      - Best accuracy when DilBERT is available
      - Cloud accuracy when OpenAI key is provided
      - Zero-dependency operation via heuristics

    The chain also records which backend was used for each classification,
    which lets you compare effectiveness across backends.

    Usage:
        chain = GateFallbackChain(
            backends=[
                DilBERTBackend(model_path=Path("~/.recall/models/distilbert-memory-gate")),
                HeuristicBackend(),  # always available
            ],
            metrics=GateMetrics(db_path=Path("~/.recall/memory.db")),
        )
        result = chain.classify("some text")
        # result.decision, result.confidence, result.backend_used
    """

    def __init__(
        self, backends: list[BaseGateBackend], metrics: Optional[GateMetrics] = None
    ):
        self.backends = backends
        self.metrics = metrics

    def classify(self, text: str) -> tuple[GateResult, str, bool]:
        """
        Classify text using the fallback chain.

        Returns:
            (GateResult, backend_name, fallback_used)
        """
        fallback_used = False

        for i, backend in enumerate(self.backends):
            try:
                result = backend.classify(text)
                # Record metrics
                if self.metrics:
                    self.metrics.record(
                        GateMetric(
                            timestamp=time.time(),
                            backend=backend.name,
                            text_preview=text[:100],
                            decision=result.decision.value,
                            confidence=result.confidence,
                            fallback_used=fallback_used,
                        )
                    )
                return result, backend.name, fallback_used
            except Exception as e:
                logger.warning(
                    f"Gate backend '{backend.name}' failed: {e}, trying next"
                )
                fallback_used = True

        # All backends failed — should never happen since HeuristicBackend can't fail
        logger.error("All gate backends failed, defaulting to ACTIVE")
        result = GateResult(decision=GateDecision.ACTIVE, confidence=0.5)
        if self.metrics:
            self.metrics.record(
                GateMetric(
                    timestamp=time.time(),
                    backend="emergency_fallback",
                    text_preview=text[:100],
                    decision="ACTIVE",
                    confidence=0.5,
                    fallback_used=True,
                )
            )
        return result, "emergency_fallback", True
