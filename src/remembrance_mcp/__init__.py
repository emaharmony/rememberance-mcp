"""Deprecated compatibility namespace for recall_mcp."""

from __future__ import annotations

import importlib
import sys
import warnings

import recall_mcp as _recall
from recall_mcp import *  # noqa: F401,F403
from recall_mcp.compat import RecallCompatibilityWarning


warnings.warn(
    "remembrance_mcp is deprecated; import recall_mcp instead.",
    RecallCompatibilityWarning,
    stacklevel=2,
)

_MODULE_ALIASES = (
    "config",
    "compat",
    "gate_backends",
    "nats_sub",
    "pipeline",
    "registry",
    "serve",
    "server",
    "api",
    "api.rest",
    "dream",
    "dream.cycle",
    "enrichment",
    "extract",
    "extract.extract",
    "gate",
    "gate.gate",
    "gate.ollama",
    "graph",
    "graph.edges",
    "graph.entity",
    "graph.traversal",
    "search",
    "search.hybrid",
    "store",
    "store.edges",
    "store.facts",
    "store.markdown",
    "store.memory",
    "store.store",
)

for _suffix in _MODULE_ALIASES:
    _module = importlib.import_module(f"recall_mcp.{_suffix}")
    sys.modules[f"{__name__}.{_suffix}"] = _module
    if "." not in _suffix:
        globals()[_suffix] = _module

__all__ = _recall.__all__


def __getattr__(name: str):
    return getattr(_recall, name)
