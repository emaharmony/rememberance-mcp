# Codebase Review Context Pack: `recall-mcp`

## Relevant files ranked by importance

1. `src/recall_mcp/search/hybrid.py` — explicitly contains vector-search placeholders; verify public search semantics and embedding/storage interfaces.
2. `src/recall_mcp/dream/cycle.py` — consolidation is explicitly a placeholder because embeddings are unavailable.
3. `src/recall_mcp/pipeline.py` and `extract/extract.py` — orchestration and Ollama-to-stub degradation behavior.
4. `src/recall_mcp/api/rest.py` and `server.py` — public REST/MCP contracts, validation, error mapping, concurrency, and authentication assumptions.
5. `src/recall_mcp/store/{memory,edges,facts,store}.py` — schema ownership, migrations, transactions, concurrency, and swallowed exceptions.
6. `src/recall_mcp/nats_sub.py` — broad exception handling and delivery/acknowledgement semantics.
7. `src/recall_mcp/compat.py`, `src/remembrance_mcp/*`, `pyproject.toml`, integration scripts — rename/backward-compatibility and packaging risk.

## Entry points and likely data flow

- CLI/service entry points: `recall_mcp.__main__`, `recall_mcp.serve`, console scripts in `pyproject.toml`.
- Protocol entry points: MCP `create_server()` and REST `RecallHandler` / `start_rest_api()`.
- Likely flow: client request -> REST/MCP adapter -> `MemoryPipeline` -> gate/extractor -> memory/entity/fact stores -> graph/search -> response/context pack. `DreamCycle` performs background maintenance/consolidation. NATS optionally supplies ingestion events.
- Legacy `remembrance_mcp` import and command shims delegate to the new `recall_mcp` package.

## Symbols and checks

- `MemoryPipeline`: verify error policy, fallback visibility, dependency lifecycle, and transaction boundaries.
- `HybridSearch`: verify keyword/vector/graph fusion, score normalization, missing embeddings, query limits, and SQL safety.
- `DreamCycle`: verify each phase, idempotency, crash recovery, and placeholder consolidation.
- `OllamaExtractor` / `StubExtractor`: verify whether fallback can silently discard useful content.
- `MemoryStoreV2`, `EntityStore`, `FactStore`: verify migrations, constraints, locking, atomicity, and data corruption handling.
- `RecallHandler`: verify JSON/body limits, timeouts, CORS/auth exposure, validation, and stable errors.
- `NatsSubscriber`: verify reconnect, redelivery, ack timing, poison messages, cancellation, and exception telemetry.

## Incomplete-feature hypotheses to verify

- Confirmed marker: semantic vector retrieval is a placeholder, so “hybrid” search may currently mean keyword plus graph only.
- Confirmed marker: dream-cycle consolidation is a placeholder awaiting embeddings.
- Deferred marker: LLM-assisted entity extraction is described as a future optional second pass.
- Risk hypothesis: automatic stub extraction may make captures appear successful while producing low-value or no extracted facts/entities.
- Risk hypothesis: rename compatibility may cover imports and CLI help but not packaged wheels, submodules, environment variables, data directories, or installed integration scripts.

## Quality and operational risks

- No visible CI workflow, type-checker configuration, coverage threshold, lint/security gate, release automation, changelog, or contributor documentation.
- Broad swallowed exceptions in subscriber, storage/search code, and hook scripts can hide data loss or degraded behavior.
- Core files are large and combine protocol, persistence, scoring, or orchestration responsibilities.
- Current tree is a large uncommitted rename; packaging and compatibility must be tested from built artifacts, not only via source-tree imports.

## Inspect next

1. Focused regions around vector placeholder and consolidation placeholder.
2. Pipeline initialization/capture/search methods and fallback behavior.
3. REST handler routing/body parsing plus MCP tool registration.
4. Store schema initialization and every broad exception handler.
5. Compatibility shims, manifest/package discovery, and integration commands.
6. Run full tests, coverage, wheel/sdist build, install/import smoke tests, and static/security checks where available.
