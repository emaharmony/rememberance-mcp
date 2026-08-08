"""
Gate Backends Registry — Extensible Backend System

PATTERN: Plugin Registry
========================

A registry is a dictionary that maps names to classes. New backends
register themselves, and the pipeline discovers them at runtime.

This is the same pattern VS Code uses for extensions, Flask uses for
blueprints, and pytest uses for plugins:

  1. Each backend is a class that implements BaseGateBackend
  2. It calls register_gate_backend("name", MyBackend) at import time
  3. The pipeline looks up backends by name from the registry
  4. Users configure which backends to use via environment variables

HOW TO ADD A CUSTOM BACKEND:
============================

Option 1: Built-in (add to this file)
  Just create a new class extending BaseGateBackend and register it.

Option 2: External plugin (separate package)
  In your own Python package:

    from recall_mcp.gate_backends import BaseGateBackend, register_gate_backend

    class MyCustomBackend(BaseGateBackend):
        @property
        def name(self) -> str:
            return "my_custom"

        def classify(self, text: str) -> GateResult:
            # Your logic here
            ...

    register_gate_backend("my_custom", MyCustomBackend)

  Then in your config:
    RECALL_GATE_BACKENDS=my_custom,heuristic

This pattern means the core package never needs to change to support
new providers. Anyone can add Anthropic Claude, Cohere, local Llama,
or a custom rule engine without forking the repo.
"""

from __future__ import annotations


from typing import Any
from recall_mcp.compat import get_env
from recall_mcp.gate_backends import (
    BaseGateBackend,
    DilBERTBackend,
    HeuristicBackend,
    OpenAIBackend,
    GateFallbackChain,
    GateMetrics,
)

# ── Backend Registry ────────────────────────────────────────────────────────

_BACKEND_REGISTRY: dict[str, type[BaseGateBackend]] = {}


def register_gate_backend(name: str, backend_class: type[BaseGateBackend]) -> None:
    """
    Register a gate backend class by name.

    Usage:
        register_gate_backend("anthropic", AnthropicBackend)

    After registration, you can reference it by name in config:
        RECALL_GATE_BACKENDS=anthropic,heuristic
    """
    _BACKEND_REGISTRY[name] = backend_class


def get_registered_backends() -> dict[str, type[BaseGateBackend]]:
    """Return all registered backend classes."""
    return dict(_BACKEND_REGISTRY)


# ── Register Built-in Backends ───────────────────────────────────────────────

register_gate_backend("dilbert", DilBERTBackend)
register_gate_backend("heuristic", HeuristicBackend)
register_gate_backend("openai", OpenAIBackend)


# ── Backend Builder ─────────────────────────────────────────────────────────


def build_gate_chain(
    backend_names: list[str] | None = None,
    settings=None,
    metrics: GateMetrics | None = None,
) -> GateFallbackChain:
    """
    Build a GateFallbackChain from a list of backend names.

    Args:
        backend_names: Ordered list of backend names to try.
                       Falls back to RECALL_GATE_BACKENDS env var.
                       Defaults to ["dilbert", "heuristic"] if neither set.
        settings: Settings object (for model paths, API keys, etc.)
        metrics: GateMetrics for effectiveness tracking

    Example:
        # Use default chain (dilbert → heuristic)
        chain = build_gate_chain()

        # Custom chain (openai → heuristic, no local model)
        chain = build_gate_chain(["openai", "heuristic"])

        # From env var
        # RECALL_GATE_BACKENDS=openai,heuristic
        chain = build_gate_chain()  # reads env var
    """
    import os

    if settings is None:
        from recall_mcp.config import Settings

        settings = Settings()

    # Determine backend list
    if backend_names is None:
        env_backends = get_env("GATE_BACKENDS", "")
        if env_backends:
            backend_names = [b.strip() for b in env_backends.split(",") if b.strip()]
        else:
            backend_names = ["dilbert", "heuristic"]  # sensible default

    registered = get_registered_backends()
    backends = []

    for name in backend_names:
        name_lower = name.lower()

        if name_lower not in registered:
            import logging

            logging.getLogger(__name__).warning(
                f"Unknown gate backend '{name_lower}', skipping. "
                f"Available: {list(registered.keys())}"
            )
            continue

        backend_class: Any = registered[name_lower]

        # Build with appropriate kwargs based on backend type
        if name_lower == "dilbert":
            try:
                backends.append(
                    backend_class(
                        model_path=settings.GATE_MODEL_PATH,
                        skip_threshold=settings.SKIP_THRESHOLD,
                    )
                )
            except Exception as e:
                import logging

                logging.getLogger(__name__).warning(f"DilBERT backend unavailable: {e}")

        elif name_lower == "openai":
            api_key = os.environ.get("OPENAI_API_KEY", "")
            if api_key:
                backends.append(backend_class(api_key=api_key))
            else:
                import logging

                logging.getLogger(__name__).warning(
                    "OpenAI backend skipped: no OPENAI_API_KEY"
                )

        elif name_lower == "heuristic":
            backends.append(backend_class())  # no config needed

        else:
            # Custom backends get settings passed through
            try:
                backends.append(backend_class(settings=settings))
            except TypeError:
                backends.append(backend_class())

    # Always append heuristic as ultimate fallback if not already included
    if "heuristic" not in [b.name for b in backends]:
        backends.append(HeuristicBackend())

    if metrics is None:
        metrics_db = settings.DB_PATH.parent / "metrics.db"
        metrics = GateMetrics(db_path=metrics_db)

    return GateFallbackChain(backends=backends, metrics=metrics)
