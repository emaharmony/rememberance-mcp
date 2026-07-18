"""
Recall MCP — Universal memory for AI agents.

ARCHITECTURE PATTERN: Three-Layer Pipeline (Cascading Classifier)
================================================================

This server uses a pattern common in production AI systems:

  Layer 1 (GATE)    → Cheap, fast model decides IF we should save
  Layer 2 (EXTRACT) → Expensive, slow model decides WHAT to save
  Layer 3 (STORE)   → Database write with tier-based TTL

WHY THIS PATTERN?
- 80%+ of conversation messages are not worth storing ("ok", "thanks", "hmm")
- Running a big model on every message is expensive and slow
- The gate model is ~100x cheaper and ~20x faster than the extract model
- This "funnel" pattern is used by Google, Meta, etc. for content moderation,
  spam detection, and recommendation filtering

KEY CONCEPTS:
- MCP (Model Context Protocol): JSON-RPC protocol that lets AI agents call tools
- Cascading Classifier: Chain of models, cheap→expensive, early-exit on rejection
- TTL (Time To Live): Auto-expiry based on importance tier (like CPU cache L1/L2/L3)
- Consolidation: Periodic garbage collection that promotes/demotes memory tiers

DESIGN DECISIONS:
- SQLite for storage: Zero-config, single-file, fast enough for local use,
  and universally available. No server process needed.
- DilBERT for gate: DistilBERT is a "distilled" (compressed) version of BERT.
  It's ~40% smaller, ~60% faster, with 97% of the accuracy.
  Fine-tuned on our 4-class dataset for memory relevance.
- Nemotron for extraction: Small but capable model for structured extraction.
  Runs locally via Ollama, no API key needed.
"""

from recall_mcp.config import Settings
from recall_mcp.gate import MemoryGate, GateDecision, GateResult
from recall_mcp.gate_backends import (
    BaseGateBackend,
    DilBERTBackend,
    HeuristicBackend,
    OpenAIBackend,
    GateFallbackChain,
    GateMetrics,
    GateMetric,
)
from recall_mcp.extract import (
    BaseExtractor,
    OllamaExtractor,
    StubExtractor,
    ExtractionResult,
)

from recall_mcp.store import MemoryStore, Memory
from recall_mcp.pipeline import MemoryPipeline
from recall_mcp.server import create_server
from recall_mcp.registry import (
    register_gate_backend,
    get_registered_backends,
    build_gate_chain,
)

# V2 exports
from recall_mcp.store.edges import EntityStore, Entity, Edge
from recall_mcp.store.facts import FactStore
from recall_mcp.store.memory import MemoryStoreV2
from recall_mcp.store.markdown import MarkdownSync
from recall_mcp.graph.entity import EntityDetector, DetectedEntity
from recall_mcp.graph.edges import GraphWiring
from recall_mcp.graph.traversal import GraphTraversal
from recall_mcp.search.hybrid import HybridSearch, SearchResult
from recall_mcp.dream.cycle import DreamCycle, ALL_PHASES
from recall_mcp.gate.ollama import OllamaGateBackend
from recall_mcp.api.rest import start_rest_api

__all__ = [
    "Settings",
    "MemoryGate",
    "GateDecision",
    "GateResult",
    "BaseGateBackend",
    "DilBERTBackend",
    "HeuristicBackend",
    "OpenAIBackend",
    "GateFallbackChain",
    "GateMetrics",
    "GateMetric",
    "register_gate_backend",
    "get_registered_backends",
    "build_gate_chain",
    "BaseExtractor",
    "OllamaExtractor",
    "StubExtractor",
    "ExtractionResult",
    "MemoryStore",
    "Memory",
    "MemoryPipeline",
    "create_server",
    "main",
    # V2
    "EntityStore",
    "Entity",
    "Edge",
    "FactStore",
    "MemoryStoreV2",
    "MarkdownSync",
    "EntityDetector",
    "DetectedEntity",
    "GraphWiring",
    "GraphTraversal",
    "HybridSearch",
    "SearchResult",
    "DreamCycle",
    "ALL_PHASES",
    "OllamaGateBackend",
    "start_rest_api",
]


def main():
    """Entry point for the `recall-mcp` stdio MCP server.

    Wires the MCP server to stdio transport. `Server.run()` requires the
    read/write streams and initialization options, so we open the stdio
    transport and pass them through.
    """
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(
        prog="recall-mcp",
        description="Recall MCP stdio server",
    )
    parser.parse_args()

    async def _serve():
        import mcp.server.stdio
        from mcp.server import NotificationOptions
        from mcp.server.models import InitializationOptions
        from recall_mcp.config import Settings

        settings = Settings.get()
        server = create_server()
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name=settings.MCP_SERVER_NAME,
                    server_version=settings.MCP_SERVER_VERSION,
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )

    asyncio.run(_serve())


if __name__ == "__main__":
    main()
