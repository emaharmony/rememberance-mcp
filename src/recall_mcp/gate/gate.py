"""
Memory Gate — DilBERT 4-Class Classifier

PATTERN: Cascading Classifier (Early Exit)
============================================

A "cascading classifier" is a chain of models where each one is more
expensive than the last, and early exits prevent unnecessary computation.

This is the FIRST model in the cascade. It's designed to be:
  - Tiny (~250MB vs ~2.5GB for the extract model)
  - Fast (<100ms inference on CPU)
  - Conservative (better to save too much than miss important data)

HOW DISTILBERT WORKS:
  BERT = Bidirectional Encoder Representations from Transformers
  DisilBERT = "Distilled" BERT (compressed via knowledge distillation)

  Knowledge distillation is like a teacher-student process:
  1. Train a big "teacher" model (BERT, 110M params)
  2. Train a small "student" model to mimic the teacher's outputs
  3. Student learns the teacher's "soft probabilities" not just hard labels
  4. Result: 40% smaller, 60% faster, 97% of accuracy

  For our 4-class task, we fine-tuned DilBERT on labeled conversation data:
  - SKIP examples: "ok", "thanks", "hmm", "hello"
  - COLD examples: casual mentions, test data, small talk
  - ACTIVE examples: project status, task assignments, current work
  - PERSIST examples: decisions, architecture, people, rules

GATE DECISION LOGIC:
  The model outputs 4 probabilities (one per class).
  We pick the class with highest probability IF it crosses a threshold.
  This prevents the model from making confident wrong predictions.

  Edge case: what if two classes are close? We default to the MORE
  important class (better to over-save than under-save).
"""

import logging
from enum import Enum
from typing import Any
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


class GateDecision(str, Enum):
    """The 4 classification classes for memory relevance."""

    SKIP = "SKIP"  # Don't store at all
    COLD = "COLD"  # Store with short TTL (1 day)
    ACTIVE = "ACTIVE"  # Store with medium TTL (30 days)
    PERSIST = "PERSIST"  # Store permanently


@dataclass
class GateResult:
    """Output of the gate classifier."""

    decision: GateDecision
    confidence: float  # 0.0 to 1.0

    @property
    def should_capture(self) -> bool:
        return self.decision != GateDecision.SKIP


class MemoryGate:
    """
    DilBERT-based 4-class memory gate.

    Usage:
        gate = MemoryGate(model_path="/path/to/model")
        result = gate.classify("Ema finished the audit")
        # → GateResult(decision=PERSIST, confidence=0.82)
        if result.should_capture:
            # proceed to extraction
    """

    def __init__(self, model_path: Path, skip_threshold: float = 0.7):
        self.model_path = model_path
        self.skip_threshold = skip_threshold
        self._model: Any = None
        self._tokenizer: Any = None

    def _load_model(self):
        """Lazy-load the model on first use (saves startup time and RAM)."""
        if self._model is not None:
            return

        # Lazy imports — don't import torch/transformers until we actually need them.
        # This means `import recall_mcp` doesn't take 10 seconds.
        from transformers import (
            DistilBertTokenizer,
            DistilBertForSequenceClassification,
        )

        if not self.model_path.exists():
            raise FileNotFoundError(
                f"Gate model not found at {self.model_path}. "
                f"Train one with scripts/train-gate.py or copy your existing model."
            )

        logger.info(f"Loading gate model from {self.model_path}")
        self._tokenizer = DistilBertTokenizer.from_pretrained(str(self.model_path))
        self._model = DistilBertForSequenceClassification.from_pretrained(
            str(self.model_path)
        )
        self._model.eval()  # Set to eval mode (disables dropout)
        # Keep on CPU — gate model is small enough, GPU overhead isn't worth it

    def classify(self, text: str) -> GateResult:
        """
        Classify a piece of text into one of 4 memory relevance classes.

        Returns a GateResult with the decision and confidence score.
        """
        self._load_model()

        import torch

        # Tokenize: convert text → numbers the model understands
        # truncation=True: cut off text longer than 512 tokens
        # (BERT models have a max input length of 512 tokens)
        inputs = self._tokenizer(
            text,
            return_tensors="pt",  # PyTorch format
            truncation=True,  # Don't exceed 512 tokens
            max_length=512,
        )

        # Inference: run the model WITHOUT computing gradients
        # torch.no_grad() saves ~50% memory and is faster (no backward pass needed)
        with torch.no_grad():
            logits = self._model(**inputs).logits

        # Softmax: convert raw logits to probabilities (0.0 to 1.0, sum to 1.0)
        probs = torch.softmax(logits, dim=-1)[0]

        # Pick the class with highest probability
        pred_class = int(torch.argmax(probs).item())
        confidence = probs[pred_class].item()

        # Map index → GateDecision enum
        # Must match the order used during training!
        labels = [
            GateDecision.SKIP,
            GateDecision.COLD,
            GateDecision.ACTIVE,
            GateDecision.PERSIST,
        ]
        decision = labels[pred_class]

        # Threshold check: if the model isn't confident enough in SKIP,
        # and the second-highest class is ACTIVE/PERSIST, promote it
        # This prevents over-aggressive filtering
        if decision == GateDecision.SKIP and confidence < self.skip_threshold:
            # Not confident it's SKIP → promote to next highest class
            probs_no_skip = probs.clone()
            probs_no_skip[0] = 0  # Zero out SKIP
            next_best = int(torch.argmax(probs_no_skip).item())
            decision = labels[next_best]
            confidence = probs[next_best].item()

        logger.debug(f"Gate: {decision.value} (confidence: {confidence:.3f})")
        return GateResult(decision=decision, confidence=confidence)
