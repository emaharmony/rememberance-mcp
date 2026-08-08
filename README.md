# Recall

Recall is persistent semantic memory infrastructure for AI agents.

Recall is a local-first memory system that gives AI agents durable context through structured extraction, hybrid retrieval, knowledge graphs, temporal facts, and maintenance workflows. It stores data in SQLite, exposes MCP and REST interfaces, and works without an external database.

## Project identity

| Surface | Canonical value |
| --- | --- |
| Product | Recall |
| Distribution | recall-mcp |
| Python package | recall_mcp |
| Primary CLI | recall |
| MCP CLI | recall-mcp |
| Service CLI | recall-service |
| Environment prefix | RECALL_ |
| Default data directory | ~/.recall |

See [Migrating to Recall](docs/migrating-to-recall.md) for the temporary compatibility layer and safe migration behavior.

## Requirements

- Python 3.10 or newer
- Optional: Ollama for local structured extraction and LLM-backed dream phases
- Optional: MCP dependencies for stdio server use
- Optional: NATS dependencies for event-bus capture

The default heuristic path needs no API key.

## Install

From a repository checkout:

~~~bash
python -m venv .venv
# Windows
.venv\Scripts\python -m pip install -e ".[mcp]"
# macOS/Linux
.venv/bin/python -m pip install -e ".[mcp]"
~~~

Useful extras:

~~~bash
python -m pip install -e ".[nats]"
python -m pip install -e ".[gate]"
python -m pip install -e ".[all]"
~~~

## Commands

~~~bash
recall --help
recall-mcp --help
recall-service --help
recall-admin --help
~~~

The stdio MCP server also runs as:

~~~bash
python -m recall_mcp
~~~

Start the combined REST/NATS service:

~~~bash
recall-service --host 127.0.0.1 --port 8788 --no-nats
# equivalent
python -m recall_mcp.serve --host 127.0.0.1 --port 8788 --no-nats
~~~

Start only the REST API:

~~~bash
python -m recall_mcp.api --host 127.0.0.1 --port 8788
~~~

Ports and REST routes are unchanged by the rename.

## MCP configuration

A canonical client configuration is:

~~~json
{
  "mcpServers": {
    "recall": {
      "command": "recall-mcp"
    }
  }
}
~~~

The MCP tools remain memory_search, memory_capture, memory_context_build, memory_get, memory_delete, memory_consolidate, memory_graph_query, memory_entity_get, memory_entity_search, and memory_dream.

## Python API

~~~python
from recall_mcp import MemoryPipeline

pipeline = MemoryPipeline()
result = pipeline.capture(
    "We decided Recall remains backed by SQLite",
    source="example",
    tier="persist",
)
context = pipeline.build_context("SQLite architecture")
~~~

Public exports from the former package remain available from recall_mcp.

## Configuration

Canonical environment variables use RECALL_. New variables always win over their legacy equivalents.

| Variable | Default | Purpose |
| --- | --- | --- |
| RECALL_HOME | resolution described below | Databases, models, and generated brain files |
| RECALL_GATE_BACKENDS | dilbert,heuristic | Ordered gate backend list |
| RECALL_URL | http://127.0.0.1:18790 in integration hooks | REST service URL |
| RECALL_TIMEOUT | integration-specific | Hook HTTP timeout |
| RECALL_INJECT_LIMIT | 8 | Maximum memories injected by hooks |
| OPENAI_API_KEY | unset | Optional OpenAI gate credential |

Home-directory resolution is deliberately migration-safe:

1. Explicit RECALL_HOME
2. Existing ~/.recall
3. Explicit legacy home variable
4. Existing legacy default directory
5. New ~/.recall

When Recall selects a legacy directory, it continues using that directory in place and emits a warning to stderr. It never moves files or creates a competing empty database.

Derived paths include:

~~~text
<RECALL_HOME>/
  memory.db
  metrics.db
  models/
  brain/
~~~

## REST API

The default standalone port is 8788. Existing integrations may continue using 18790.

| Method | Route | Purpose |
| --- | --- | --- |
| GET | /health | Service health |
| GET | /health/live | Process liveness |
| GET | /health/ready | Local dependency readiness |
| GET | /metrics | Authenticated Prometheus metrics |
| POST | /capture | Capture text |
| GET | /search | Search memories |
| POST | /context/build | Build task context |
| GET | /memory/{id} | Read a memory |
| DELETE | /memory/{id} | Delete a memory |
| POST | /dream | Run maintenance phases |

The Prism /v1 compatibility routes remain unchanged.

Configure RECALL_API_TOKEN_FILE for production and send its bearer token on every data, administration, and metrics request.

~~~bash
curl http://127.0.0.1:8788/health
curl -X POST http://127.0.0.1:8788/capture -H "Content-Type: application/json" -d '{"text":"Recall uses SQLite","source":"example"}'
curl "http://127.0.0.1:8788/search?q=SQLite&mode=keyword"
~~~

## Optional gate model

Install gate dependencies and download the existing release asset:

~~~bash
python -m pip install -e ".[gate]"
bash scripts/download-dilbert.sh
~~~

The downloader uses the safe home-resolution order and defaults new installations to ~/.recall/models/distilbert-memory-gate. Release assets are hosted under the renamed emaharmony/recall repository.

## Integrations

- One-command setup for any agent: `recall-admin install-hooks --agent claude-code|codex|all` registers the MCP server and hooks (and migrates a stale `remembrance` registration) with no source checkout and no hand-editing of JSON. Run with `--dry-run` first to preview.
- Claude Code: [integration guide](integrations/claude-code/README.md)
- Codex: [integration guide](integrations/codex/README.md)
- Windows hidden REST startup: start_recall_rest.ps1 and start_recall_rest.vbs
- Claude stdio wrapper: recall_mcp_stdio.cmd

Deprecated script names remain as small delegating wrappers during the compatibility window.

## Compatibility

The migration layer currently preserves:

- import remembrance_mcp and documented nested imports
- python -m remembrance_mcp
- remembrance-mcp and remembrance-service
- REMEMBRANCE_* environment fallbacks
- existing ~/.remembrance databases in place
- old Windows and Claude wrapper filenames
- unchanged REST routes, ports, MCP tool names, NATS behavior, table names, and memory IDs

Warnings use stderr and never write to MCP protocol stdout.

## Production deployment

Recall 2.1 ships a pinned Ubuntu Compose stack and a native Windows service installer. Both keep Recall, Ollama, and NATS on loopback and use Tailscale Serve for private HTTPS.

- [Architecture](docs/architecture.md)
- [Ubuntu deployment](docs/deployment/ubuntu.md)
- [Windows deployment](docs/deployment/windows.md)
- [Upgrade to 2.1](docs/upgrading-to-2.1.md)
- [Configuration](docs/configuration.md)
- [Security model](docs/security.md)
- [Operations and monitoring](docs/operations.md)
- [Backup and restore](docs/backup-restore.md)
- [Release readiness and remaining gates](docs/release-readiness.md)
- [Production certification plan](docs/production-certification-plan.md)
- [REST examples](docs/api-examples.md)
- [Changelog](CHANGELOG.md)

## Development

~~~bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m compileall src
python -m build
~~~

The package build must be installed into a clean temporary environment before release. Smoke-test both canonical and legacy commands from the installed wheel.

## License

Apache-2.0. See [LICENSE](LICENSE).