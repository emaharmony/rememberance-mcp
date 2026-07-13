"""
REST API — HTTP Interface for Remembrance

PATTERN: REST API (Thin HTTP Wrapper)
========================================

This module provides a REST API alongside the MCP protocol.
It wraps the same Pipeline methods as the MCP server but
exposes them as HTTP endpoints.

WHY BOTH MCP AND REST?
- MCP: For AI agents that speak MCP (Claude, Cursor, etc.)
- REST: For web apps, CLIs, scripts, and Prism's Go client
- Some consumers prefer simple HTTP over MCP's JSON-RPC

ENDPOINTS:
  POST /capture              → gate → extract → graph → enrich → store
  GET  /search?q=&mode=      → hybrid search
  GET  /memory/:id           → memory + entities + edges
  GET  /entity/:slug         → compiled truth + timeline + edges
  GET  /graph/:slug?depth=N  → N-hop graph traversal
  POST /dream                → trigger dream cycle
  GET  /context/build?task=&project=&agent=  → context building
  GET  /health               → health check
  GET  /stats                → subsystem stats

PRISM /v1 COMPATIBILITY LAYER:
  Prism's Go remembrance client speaks a `/v1/*` dialect. These aliases map
  it onto the same pipeline so remembrance-mcp is a drop-in replacement for
  the in-repo `remembrance/` service:
  GET  /v1/health            → health check
  POST /v1/memory/ingest     → capture (CaptureRequest shape)
  POST /v1/context/build     → context build returning `context_markdown`
  POST /v1/dream             → trigger dream cycle
"""

from __future__ import annotations

import errno
import json
import logging
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

from remembrance_mcp.pipeline import MemoryPipeline

logger = logging.getLogger(__name__)


CLIENT_DISCONNECT_WINERRORS = {10053, 10054}
CLIENT_DISCONNECT_ERRNOS = {
    errno.EPIPE,
    errno.ECONNABORTED,
    errno.ECONNRESET,
}


def _is_client_disconnect(error: BaseException) -> bool:
    """Return True when the client closed the socket before we finished writing."""
    if isinstance(error, (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)):
        return True
    if not isinstance(error, OSError):
        return False

    winerror = getattr(error, "winerror", None)
    if winerror in CLIENT_DISCONNECT_WINERRORS:
        return True

    return error.errno in CLIENT_DISCONNECT_ERRNOS


def _build_context_pack(
    ctx: dict, task: str, project: Optional[str], agent: Optional[str], max_tokens: int
) -> dict:
    """Translate pipeline.build_context() output into the ContextPack shape
    that Prism's Go client (POST /v1/context/build) expects.

    Prism only injects the ``context_markdown`` field, so the markdown is the
    important part; the structured fields are provided for completeness.
    """
    memories = ctx.get("memories", []) or []
    entities = ctx.get("entities", []) or []
    threads = ctx.get("open_threads", []) or []

    lines: list[str] = []
    selected_ids: list[str] = []
    mem_details: list[dict] = []

    if memories:
        lines.append("## Relevant Memory")
        for m in memories:
            mem_id = m.get("id", "")
            text = (m.get("summary") or m.get("content") or "").strip()
            if not text:
                continue
            tag = m.get("category") or m.get("tier") or ""
            suffix = f" _({tag})_" if tag else ""
            lines.append(f"- {text}{suffix}")
            if mem_id:
                selected_ids.append(mem_id)
            mem_details.append(
                {
                    "memory_id": mem_id,
                    "title": m.get("category") or "memory",
                    "summary": text,
                    "score": float(m.get("score", 0.0) or 0.0),
                    "reason": "hybrid_search",
                }
            )
        lines.append("")

    if entities:
        lines.append("## Entities")
        for e in entities:
            truth = (e.get("compiled_truth") or "").strip()
            etype = e.get("type", "")
            head = f"**{e.get('name', '')}**"
            if etype:
                head += f" ({etype})"
            lines.append(f"- {head}: {truth}" if truth else f"- {head}")
        lines.append("")

    if threads:
        lines.append("## Open Threads")
        for t in threads:
            lines.append(f"- {t.get('entity', '')}: {(t.get('context', '') or '').strip()}")
        lines.append("")

    markdown = "\n".join(lines).strip()
    # Rough token estimate (~4 chars/token), capped to the requested budget.
    token_count = min(max_tokens, len(markdown) // 4) if markdown else 0

    return {
        "project_id": project or "prism",
        "agent_id": agent or "",
        "task": task,
        "selected_memories": selected_ids,
        "context_markdown": markdown,
        "context_json": {
            "project_id": project or "prism",
            "agent_id": agent or "",
            "task": task,
            "selected_memories": mem_details,
            "total_memories": len(mem_details),
        },
        "warnings": [],
        "token_count": token_count,
    }


class RemembranceHandler(BaseHTTPRequestHandler):
    """
    HTTP request handler for the Remembrance REST API.

    All responses are JSON. Errors return {"error": "message"}.
    """

    pipeline: MemoryPipeline = None  # Set before server starts

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)

        try:
            if path in ("/health", "/v1/health"):
                self._json_response({"status": "ok", "version": "2.0.0", **self._fts_health()})

            elif path == "/stats":
                stats = self.pipeline.stats()
                self._json_response(stats)

            elif path == "/search":
                query = params.get("q", [""])[0]
                mode = params.get("mode", ["balanced"])[0]
                category = params.get("category", [None])[0]
                tier = params.get("tier", [None])[0]
                limit = int(params.get("limit", ["10"])[0])

                if not query:
                    self._json_response({"error": "Missing query parameter 'q'"}, status=400)
                    return

                results = self.pipeline.hybrid_search.search(
                    query, mode=mode, category=category, tier=tier, limit=limit
                )
                self._json_response({"results": results, "count": len(results)})

            elif path.startswith("/memory/"):
                mem_id = path.split("/")[-1]
                memory = self.pipeline.get(mem_id)
                if not memory:
                    self._json_response({"error": f"Memory {mem_id} not found"}, status=404)
                    return
                # Also include entities
                entities = self.pipeline.entity_store.get_memory_entities(mem_id)
                memory["entities"] = entities
                self._json_response(memory)

            elif path.startswith("/entity/"):
                slug = path.split("/")[-1]
                entity = self.pipeline.entity_store.find_entity(slug)
                if not entity:
                    self._json_response({"error": f"Entity '{slug}' not found"}, status=404)
                    return
                # Include edges
                edges = self.pipeline.entity_store.get_edges(entity["id"])
                entity["edges"] = edges
                self._json_response(entity)

            elif path.startswith("/graph/"):
                slug = path.split("/")[-1]
                depth = int(params.get("depth", ["1"])[0])
                edge_types = params.get("edge_types", None)
                if edge_types:
                    edge_types = edge_types[0].split(",")

                result = self.pipeline.graph_query(slug, depth=depth, edge_types=edge_types)
                if "error" in result:
                    self._json_response(result, status=404)
                    return
                self._json_response(result)

            elif path == "/context/build":
                task = params.get("task", [""])[0]
                project = params.get("project", [None])[0]
                agent = params.get("agent", [None])[0]
                limit = int(params.get("limit", ["10"])[0])

                if not task:
                    self._json_response({"error": "Missing query parameter 'task'"}, status=400)
                    return

                context = self.pipeline.build_context(
                    task=task, project=project, agent=agent, limit=limit
                )
                self._json_response(context)

            else:
                self._json_response({"error": "Not found"}, status=404)

        except Exception as e:
            if _is_client_disconnect(e):
                logger.debug(f"GET {path} client disconnected before response was sent")
                return
            logger.error(f"GET {path} error: {e}", exc_info=True)
            self._safe_json_response({"error": str(e)}, status=500)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        try:
            body = self._read_body()

            if path == "/capture":
                text = body.get("text", "")
                source = body.get("source", "api")
                category = body.get("category")
                tier = body.get("tier")

                if not text:
                    self._json_response({"error": "Missing 'text' field"}, status=400)
                    return

                result = self.pipeline.capture(
                    text=text, source=source, category=category, tier=tier
                )
                self._json_response(result, status=201)

            elif path in ("/dream", "/v1/dream"):
                phases = body.get("phases")
                dry_run = body.get("dry_run", False)
                result = self.pipeline.dream(phases=phases, dry_run=dry_run)
                self._json_response(result)

            # ── Prism /v1 compatibility layer ──────────────────────
            # Prism's Go client posts to these /v1 paths with its own
            # request shape; translate them onto the pipeline.

            elif path == "/v1/memory/ingest":
                # Prism CaptureRequest → pipeline.capture()
                text = body.get("content") or body.get("summary") or ""
                source = (
                    body.get("source_agent")
                    or body.get("source_type")
                    or body.get("source")
                    or "prism"
                )
                category = body.get("category")
                # NOTE: Prism's `scope` (project/user) is a different vocabulary
                # from the gate tier (cold/active/persist), so we let the gate
                # decide the tier rather than forcing scope onto it.
                if not text:
                    self._json_response({"error": "Missing 'content' field"}, status=400)
                    return
                result = self.pipeline.capture(text=text, source=source, category=category)
                self._json_response(result, status=201)

            elif path == "/v1/context/build":
                # Prism BuildContextRequest → pipeline.build_context()
                task = body.get("task", "")
                project = body.get("project_id") or body.get("project")
                agent = body.get("agent_id") or body.get("agent")
                max_tokens = int(body.get("max_tokens") or 2500)
                limit = int(body.get("limit") or 10)
                if not task:
                    self._json_response({"error": "Missing 'task' field"}, status=400)
                    return
                ctx = self.pipeline.build_context(
                    task=task, project=project, agent=agent, limit=limit
                )
                self._json_response(_build_context_pack(ctx, task, project, agent, max_tokens))

            else:
                self._json_response({"error": "Not found"}, status=404)

        except Exception as e:
            if _is_client_disconnect(e):
                logger.debug(f"POST {path} client disconnected before response was sent")
                return
            logger.error(f"POST {path} error: {e}", exc_info=True)
            self._safe_json_response({"error": str(e)}, status=500)

    # ── Helpers ────────────────────────────────────────────────

    def _json_response(self, data: dict, status: int = 200):
        """Send a JSON response."""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2, default=str).encode("utf-8"))

    def _safe_json_response(self, data: dict, status: int = 200):
        """Best-effort JSON response for error paths."""
        try:
            self._json_response(data, status=status)
        except Exception as e:
            if _is_client_disconnect(e):
                logger.debug("Client disconnected before error response was sent")
                return
            raise

    def _read_body(self) -> dict:
        """Read and parse the request body as JSON."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        body = self.rfile.read(content_length)
        return json.loads(body.decode("utf-8"))

    def _fts_health(self) -> dict:
        """Cheap self-probe surfaced on /health.

        Reports the memory count and whether the FTS5 index actually returns
        the most recent memory by a token from its own content. A `False`
        here means keyword search / context build will silently return nothing
        (the external-content index drifted) — a failure mode that is otherwise
        invisible. Best-effort: never raises into the health response.
        """
        try:
            db = self.pipeline.store.db_path
            conn = sqlite3.connect(str(db))
            conn.row_factory = sqlite3.Row
            try:
                memories = conn.execute("SELECT count(*) FROM memories").fetchone()[0]
                fts_ok = True
                if memories > 0:
                    row = conn.execute(
                        "SELECT content FROM memories ORDER BY rowid DESC LIMIT 1"
                    ).fetchone()
                    token = ""
                    for word in (row["content"] or "").split():
                        cleaned = "".join(ch for ch in word if ch.isalnum())
                        if len(cleaned) >= 4:
                            token = cleaned
                            break
                    if token:
                        hit = conn.execute(
                            "SELECT 1 FROM memories_fts WHERE memories_fts MATCH ? LIMIT 1",
                            [token],
                        ).fetchone()
                        fts_ok = hit is not None
                return {"memories": memories, "fts_ok": fts_ok}
            finally:
                conn.close()
        except Exception as exc:
            return {"health_probe_error": str(exc)}

    def log_message(self, format, *args):
        """Override to use our logger instead of stderr."""
        logger.debug(f"REST API: {format % args}")


def start_rest_api(pipeline: MemoryPipeline, host: str = "127.0.0.1", port: int = 8788):
    """
    Start the REST API server.

    Args:
        pipeline: MemoryPipeline instance
        host: Bind address (default: all interfaces)
        port: Port number (default: 8788, matching Prism's /context/build convention)
    """
    RemembranceHandler.pipeline = pipeline
    # Threaded server: a single slow request (e.g. an Ollama-backed capture or
    # a balanced context build) must not block health checks and concurrent
    # readers. SQLite WAL + the default busy timeout handle concurrent access.
    server = ThreadingHTTPServer((host, port), RemembranceHandler)
    server.daemon_threads = True
    logger.info(f"REST API starting on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("REST API shutting down")
        server.server_close()
