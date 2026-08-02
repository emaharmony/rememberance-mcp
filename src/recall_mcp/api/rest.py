"""
REST API — HTTP Interface for Recall

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
  Prism's Go recall client speaks a `/v1/*` dialect. These aliases map
  it onto the same pipeline so recall-mcp is a drop-in replacement for
  the in-repo `recall/` service:
  GET  /v1/health            → health check
  POST /v1/memory/ingest     → capture (CaptureRequest shape)
  POST /v1/context/build     → context build returning `context_markdown`
  POST /v1/dream             → trigger dream cycle
"""

from __future__ import annotations

import json
import logging
import errno
import sqlite3
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from typing import Optional

from recall_mcp.pipeline import MemoryPipeline
from recall_mcp.continuity import ContinuityError
from recall_mcp.api.security import (
    ServiceMetrics,
    SlidingWindowRateLimiter,
    TokenAuthenticator,
    is_loopback_address,
    verify_token_file_permissions,
)

logger = logging.getLogger(__name__)


CLIENT_DISCONNECT_WINERRORS = {10053, 10054}
CLIENT_DISCONNECT_ERRNOS = {
    errno.EPIPE,
    errno.ECONNABORTED,
    errno.ECONNRESET,
}


def _is_client_disconnect(error: BaseException) -> bool:
    """Return True when the client closed the socket before we finished writing."""
    if isinstance(
        error, (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)
    ):
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
    active_task = ctx.get("active_task")
    checkpoint = ctx.get("latest_checkpoint")
    delta = ctx.get("session_delta")

    lines: list[str] = []
    selected_ids: list[str] = []
    mem_details: list[dict] = []

    if active_task:
        lines.extend(
            [
                "## Active Task",
                f"- **{active_task.get('title', '')}**: {active_task.get('objective', '')}",
                f"- Status: {active_task.get('status', '')}",
                "",
            ]
        )

    if checkpoint:
        lines.append(f"## Session Checkpoint v{checkpoint.get('version', 0)}")
        if checkpoint.get("summary"):
            lines.append(f"- {checkpoint['summary']}")
        for label, key in (
            ("Constraints", "constraints"),
            ("Blockers", "blockers"),
            ("Open questions", "open_questions"),
        ):
            values = checkpoint.get(key, []) or []
            if values:
                lines.append(f"- {label}: " + "; ".join(str(value) for value in values))
        lines.append("")

    if delta and not delta.get("no_change"):
        lines.append(
            f"## Session Delta v{delta.get('from_version')}→v{delta.get('to_version')}"
        )
        for event in delta.get("events", []):
            lines.append(
                f"- {event.get('event_type')} by {event.get('agent_id')}: "
                f"{json.dumps(event.get('payload', {}), sort_keys=True)}"
            )
        lines.append("")

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
            lines.append(
                f"- {t.get('entity', '')}: {(t.get('context', '') or '').strip()}"
            )
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
            "active_task": active_task,
            "latest_checkpoint": checkpoint,
            "session_delta": delta,
            "approved_decisions": ctx.get("approved_decisions", []),
            "critical_constraints": ctx.get("critical_constraints", []),
            "open_blockers": ctx.get("open_blockers", []),
            "open_questions": ctx.get("open_questions", []),
        },
        "warnings": ctx.get("warnings", []),
        "token_count": token_count,
    }


class APIError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _continuity_status(error: ContinuityError) -> int:
    if error.code == "not_found":
        return 404
    if error.code in {"idempotency_conflict", "scope_mismatch"}:
        return 409
    return 400


def _required_text(body: dict, name: str, *, alias: str | None = None) -> str:
    value = body.get(name)
    if value is None and alias is not None:
        value = body.get(alias)
    if not isinstance(value, str) or not value.strip():
        raise APIError(f"Missing '{name}' field")
    return value.strip()


class RecallHandler(BaseHTTPRequestHandler):
    """
    HTTP request handler for the Recall REST API.

    All responses are JSON. Errors return {"error": "message"}.
    """

    pipeline: MemoryPipeline = None  # type: ignore[assignment]

    service_metrics = ServiceMetrics()
    rate_limiter = None

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)
        if not self._guard_request("read", path):
            return

        try:
            if path == "/health/live":
                self._json_response({"status": "ok", "version": "2.1.0"})

            elif path in ("/health", "/v1/health"):
                self._json_response(
                    {"status": "ok", "version": "2.0.0", **self._fts_health()}
                )

            elif path == "/health/ready":
                probe = self._fts_health()
                ready = probe.get("fts_ok", False) and not probe.get(
                    "health_probe_error"
                )
                storage = self.pipeline.store.integrity_report(quick=True)
                probe["storage"] = storage
                ready = ready and bool(storage["ok"])
                operations = self.pipeline.store.operational_stats()
                probe["outbox"] = operations["outbox"]
                probe["outbox_active_leases"] = operations["outbox_active_leases"]
                probe["outbox_oldest_due_seconds"] = operations[
                    "outbox_oldest_due_seconds"
                ]
                outbox_worker = self.pipeline.outbox_dispatcher.health()
                probe["outbox_worker"] = outbox_worker
                ready = ready and bool(
                    outbox_worker["running"] and outbox_worker["thread_alive"]
                )
                if self.pipeline.settings.API_TOKEN_FILE is not None:
                    token_permissions_ok = verify_token_file_permissions(
                        self.pipeline.settings.API_TOKEN_FILE
                    )
                    probe["token_file_permissions_ok"] = token_permissions_ok
                    ready = ready and token_permissions_ok
                nats_sub = getattr(self.pipeline, "nats_sub", None)
                if nats_sub is not None:
                    nats_health = nats_sub.health()
                    probe["nats"] = nats_health
                    ready = ready and bool(nats_health.get("connected"))
                self._json_response(
                    {"status": "ready" if ready else "not_ready", **probe},
                    status=200 if ready else 503,
                )

            elif path == "/metrics":
                self.service_metrics.set_gauge(
                    "recall_memories", float(self.pipeline.store.count())
                )
                operations = self.pipeline.store.operational_stats()
                for status, count in operations["embeddings"].items():
                    self.service_metrics.set_gauge(
                        f"recall_embeddings_{status}", float(count)
                    )
                for status, count in operations["ingest_events"].items():
                    self.service_metrics.set_gauge(
                        f"recall_ingest_events_{status}", float(count)
                    )
                for status, count in operations["outbox"].items():
                    self.service_metrics.set_gauge(
                        f"recall_outbox_{status}", float(count)
                    )
                self.service_metrics.set_gauge(
                    "recall_outbox_active_leases",
                    float(operations["outbox_active_leases"]),
                )
                self.service_metrics.set_gauge(
                    "recall_outbox_oldest_due_seconds",
                    float(operations["outbox_oldest_due_seconds"]),
                )
                worker_health = self.pipeline.outbox_dispatcher.health()
                self.service_metrics.set_gauge(
                    "recall_outbox_worker_alive",
                    1.0 if worker_health["thread_alive"] else 0.0,
                )
                self.service_metrics.set_gauge(
                    "recall_outbox_dispatcher_error",
                    1.0 if worker_health["last_error"] else 0.0,
                )
                nats_sub = getattr(self.pipeline, "nats_sub", None)
                if nats_sub is not None:
                    nats_health = nats_sub.health()
                    self.service_metrics.set_gauge(
                        "recall_nats_connected",
                        1.0 if nats_health["connected"] else 0.0,
                    )
                self._text_response(
                    self.service_metrics.render(),
                    "text/plain; version=0.0.4; charset=utf-8",
                )

            elif path == "/stats":
                stats = self.pipeline.stats()
                self._json_response(stats)

            elif path.startswith("/v2/tasks/") and len(path.split("/")) == 4:
                task_id = path.split("/")[3]
                task_result = self.pipeline.task_service.get_task(
                    task_id,
                    user_id=params.get("user_id", [None])[0],
                    project_id=params.get("project_id", [None])[0],
                    repository_id=params.get("repository_id", [None])[0],
                )
                self._json_response(task_result)

            elif path.startswith("/v2/sessions/") and path.endswith("/delta"):
                session_id = path.split("/")[3]
                known_version = self._bounded_int(
                    params.get("known_version", ["0"])[0],
                    default=0,
                    minimum=0,
                    maximum=2_147_483_647,
                    name="known_version",
                )
                delta = self.pipeline.session_service.get_delta(
                    session_id,
                    known_version=known_version,
                    agent_id=params.get("agent_id", [None])[0],
                )
                self._json_response(delta)

            elif path.startswith("/v2/sessions/") and len(path.split("/")) == 4:
                session_id = path.split("/")[3]
                self._json_response(
                    self.pipeline.session_service.get_session(session_id)
                )

            elif path == "/search":
                query = params.get("q", [""])[0]
                mode = params.get("mode", ["balanced"])[0]
                category = params.get("category", [None])[0]
                tier = params.get("tier", [None])[0]
                project = params.get("project", [None])[0]
                agent = params.get("agent", [None])[0]
                search_task_id = params.get("task_id", [None])[0]
                search_session_id = params.get("session_id", [None])[0]
                limit = self._bounded_int(
                    params.get("limit", ["10"])[0],
                    default=10,
                    minimum=1,
                    maximum=self.pipeline.settings.MAX_RESULTS,
                    name="limit",
                )

                if not query:
                    self._json_response(
                        {"error": "Missing query parameter 'q'"}, status=400
                    )
                    return

                results = self.pipeline.hybrid_search.search(
                    query,
                    mode=mode,
                    category=category,
                    tier=tier,
                    limit=limit,
                    project=project,
                    agent=agent,
                    user_id=params.get("user_id", [None])[0],
                    workspace_id=params.get("workspace_id", [None])[0],
                    project_id=params.get("project_id", [None])[0],
                    repository_id=params.get("repository_id", [None])[0],
                    task_id=search_task_id,
                    session_id=search_session_id,
                )
                self._json_response({"results": results, "count": len(results)})

            elif path.startswith("/memory/"):
                mem_id = path.split("/")[-1]
                memory = self.pipeline.get(mem_id)
                if not memory:
                    self._json_response(
                        {"error": f"Memory {mem_id} not found"}, status=404
                    )
                    return
                # Also include entities
                entities = self.pipeline.entity_store.get_memory_entities(mem_id)
                memory["entities"] = entities
                self._json_response(memory)

            elif path.startswith("/entity/"):
                slug = path.split("/")[-1]
                entity = self.pipeline.entity_get(slug)
                if not entity:
                    self._json_response(
                        {"error": f"Entity '{slug}' not found"}, status=404
                    )
                    return
                # Include edges
                edges = self.pipeline.entity_store.get_edges(entity["id"])
                entity["edges"] = edges
                self._json_response(entity)

            elif path.startswith("/graph/"):
                slug = path.split("/")[-1]
                depth = self._bounded_int(
                    params.get("depth", ["1"])[0],
                    default=1,
                    minimum=1,
                    maximum=self.pipeline.settings.MAX_GRAPH_DEPTH,
                    name="depth",
                )
                edge_types = params.get("edge_types", None)
                if edge_types:
                    edge_types = edge_types[0].split(",")

                result = self.pipeline.graph_query(
                    slug, depth=depth, edge_types=edge_types
                )
                if "error" in result:
                    self._json_response(result, status=404)
                    return
                self._json_response(result)

            elif path == "/context/build":
                task_query = params.get("task", [""])[0]
                project = params.get("project", [None])[0]
                agent = params.get("agent", [None])[0]
                context_task_id = params.get("task_id", [None])[0]
                context_session_id = params.get("session_id", [None])[0]
                limit = self._bounded_int(
                    params.get("limit", ["10"])[0],
                    default=10,
                    minimum=1,
                    maximum=self.pipeline.settings.MAX_RESULTS,
                    name="limit",
                )

                if not task_query and not context_task_id:
                    self._json_response(
                        {"error": "Missing query parameter 'task'"}, status=400
                    )
                    return

                context = self.pipeline.build_context(
                    task=task_query,
                    project=project,
                    agent=agent,
                    limit=limit,
                    user_id=params.get("user_id", [None])[0],
                    workspace_id=params.get("workspace_id", [None])[0],
                    project_id=params.get("project_id", [None])[0],
                    repository_id=params.get("repository_id", [None])[0],
                    task_id=context_task_id,
                    session_id=context_session_id,
                    agent_id=params.get("agent_id", [None])[0],
                    known_checkpoint_version=(
                        self._bounded_int(
                            params["known_checkpoint_version"][0],
                            default=0,
                            minimum=0,
                            maximum=2_147_483_647,
                            name="known_checkpoint_version",
                        )
                        if "known_checkpoint_version" in params
                        else None
                    ),
                )
                self._json_response(context)

            else:
                self._json_response({"error": "Not found"}, status=404)

        except APIError as error:
            self._safe_json_response({"error": str(error)}, status=error.status)
        except ContinuityError as error:
            self._safe_json_response(
                {"error": str(error), "code": error.code},
                status=_continuity_status(error),
            )
        except Exception as e:
            if _is_client_disconnect(e):
                logger.debug(f"GET {path} client disconnected before response was sent")
                return
            logger.error(f"GET {path} error: {e}", exc_info=True)
            self._safe_json_response({"error": "Internal server error"}, status=500)

    def do_PATCH(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if not self._guard_request("write", path):
            return
        try:
            body = self._read_body()
            if path.startswith("/v2/tasks/") and len(path.split("/")) == 4:
                task_id = path.split("/")[3]
                result = self.pipeline.task_service.update_task(
                    task_id,
                    title=body.get("title"),
                    objective=body.get("objective"),
                    status=body.get("status"),
                )
                self._json_response(result)
            else:
                self._json_response({"error": "Not found"}, status=404)
        except ContinuityError as error:
            self._safe_json_response(
                {"error": str(error), "code": error.code},
                status=_continuity_status(error),
            )
        except APIError as error:
            self._safe_json_response({"error": str(error)}, status=error.status)
        except Exception:
            logger.exception("PATCH %s error", path)
            self._safe_json_response({"error": "Internal server error"}, status=500)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if not self._guard_request("write", path):
            return

        try:
            body = self._read_body()

            if path == "/v2/tasks":
                result = self.pipeline.task_service.create_task(
                    user_id=_required_text(body, "user_id"),
                    workspace_id=_required_text(body, "workspace_id"),
                    project_id=_required_text(body, "project_id"),
                    repository_id=_required_text(body, "repository_id"),
                    title=_required_text(body, "title"),
                    objective=_required_text(body, "objective"),
                    created_by=_required_text(body, "created_by", alias="agent_id"),
                    status=body.get("status", "active"),
                    idempotency_key=body.get("idempotency_key"),
                    agent_name=body.get("agent_name"),
                    agent_system_type=body.get("agent_system_type", "other"),
                    workspace_name=body.get("workspace_name"),
                    project_name=body.get("project_name"),
                    repository_name=body.get("repository_name"),
                    canonical_path=body.get("canonical_path"),
                    remote_url=body.get("remote_url"),
                    default_branch=body.get("default_branch"),
                )
                self._json_response(result, status=201)

            elif path == "/v2/sessions":
                result = self.pipeline.session_service.start_session(
                    task_id=_required_text(body, "task_id"),
                    agent_id=_required_text(body, "agent_id"),
                    role=body.get("role", "owner"),
                    idempotency_key=body.get("idempotency_key"),
                    agent_name=body.get("agent_name"),
                    agent_system_type=body.get("agent_system_type", "other"),
                )
                self._json_response(result, status=201)

            elif path.startswith("/v2/sessions/") and path.endswith("/join"):
                session_id = path.split("/")[3]
                result = self.pipeline.session_service.join_session(
                    session_id,
                    agent_id=_required_text(body, "agent_id"),
                    role=body.get("role", "implementer"),
                    idempotency_key=body.get("idempotency_key"),
                    agent_name=body.get("agent_name"),
                    agent_system_type=body.get("agent_system_type", "other"),
                )
                self._json_response(result)

            elif path.startswith("/v2/sessions/") and path.endswith("/events"):
                session_id = path.split("/")[3]
                result = self.pipeline.session_service.append_event(
                    session_id,
                    agent_id=_required_text(body, "agent_id"),
                    event_type=_required_text(body, "event_type"),
                    payload=body.get("payload"),
                    idempotency_key=body.get("idempotency_key"),
                )
                self._json_response(result, status=201)

            elif path.startswith("/v2/sessions/") and path.endswith("/checkpoint"):
                session_id = path.split("/")[3]
                result = self.pipeline.session_service.create_checkpoint(
                    session_id,
                    agent_id=_required_text(body, "agent_id"),
                    summary=body.get("summary", ""),
                    completed=body.get("completed"),
                    remaining=body.get("remaining"),
                    constraints=body.get("constraints"),
                    approved_decisions=body.get("approved_decisions"),
                    proposed_decisions=body.get("proposed_decisions"),
                    open_questions=body.get("open_questions"),
                    blockers=body.get("blockers"),
                    important_files=body.get("important_files"),
                    known_failures=body.get("known_failures"),
                    idempotency_key=body.get("idempotency_key"),
                    generated=bool(body.get("generated", False)),
                )
                self._json_response(result, status=201)

            elif path.startswith("/v2/sessions/") and path.endswith("/close"):
                session_id = path.split("/")[3]
                result = self.pipeline.session_service.close_session(
                    session_id,
                    agent_id=_required_text(body, "agent_id"),
                    summary=body.get("summary"),
                    idempotency_key=body.get("idempotency_key"),
                )
                self._json_response(result)

            elif path == "/capture":
                text = body.get("text", "")
                source = body.get("source", "api")
                category = body.get("category")
                tier = body.get("tier")
                project = body.get("project")
                agent = body.get("agent")

                if not text:
                    self._json_response({"error": "Missing 'text' field"}, status=400)
                    return

                if len(text) > self.pipeline.settings.MAX_CAPTURE_CHARS:
                    raise APIError(
                        "Capture exceeds RECALL_MAX_CAPTURE_CHARS", status=413
                    )
                result = self.pipeline.capture(
                    text=text,
                    source=source,
                    category=category,
                    tier=tier,
                    project=project,
                    agent=agent,
                    user_id=body.get("user_id"),
                    workspace_id=body.get("workspace_id"),
                    project_id=body.get("project_id"),
                    repository_id=body.get("repository_id"),
                    task_id=body.get("task_id"),
                    session_id=body.get("session_id"),
                )
                self._json_response(result, status=201)

            elif path == "/context/build":
                task = body.get("task", "")
                project = body.get("project")
                agent = body.get("agent")
                task_id = body.get("task_id")
                limit = self._bounded_int(
                    body.get("limit"),
                    default=10,
                    minimum=1,
                    maximum=self.pipeline.settings.MAX_RESULTS,
                    name="limit",
                )
                if not task and not task_id:
                    raise APIError("Missing 'task' field")
                context = self.pipeline.build_context(
                    task=task,
                    project=project,
                    agent=agent,
                    limit=limit,
                    user_id=body.get("user_id"),
                    workspace_id=body.get("workspace_id"),
                    project_id=body.get("project_id"),
                    repository_id=body.get("repository_id"),
                    task_id=task_id,
                    session_id=body.get("session_id"),
                    agent_id=body.get("agent_id"),
                    known_checkpoint_version=body.get("known_checkpoint_version"),
                )
                self._json_response(context)
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
                project = body.get("project_id") or body.get("project")
                agent = body.get("agent_id") or body.get("agent")
                # NOTE: Prism's `scope` (project/user) is a different vocabulary
                # from the gate tier (cold/active/persist), so we let the gate
                # decide the tier rather than forcing scope onto it.
                if not text:
                    self._json_response(
                        {"error": "Missing 'content' field"}, status=400
                    )
                    return
                if len(text) > self.pipeline.settings.MAX_CAPTURE_CHARS:
                    raise APIError(
                        "Capture exceeds RECALL_MAX_CAPTURE_CHARS", status=413
                    )
                result = self.pipeline.capture(
                    text=text,
                    source=source,
                    category=category,
                    project=project,
                    agent=agent,
                    tier=None,
                )
                self._json_response(result, status=201)

            elif path == "/v1/context/build":
                # Prism BuildContextRequest → pipeline.build_context()
                task = body.get("task", "")
                project = body.get("project_id") or body.get("project")
                agent = body.get("agent_id") or body.get("agent")
                max_tokens = self._bounded_int(
                    body.get("max_tokens"),
                    default=2500,
                    minimum=1,
                    maximum=100_000,
                    name="max_tokens",
                )
                limit = self._bounded_int(
                    body.get("limit"),
                    default=10,
                    minimum=1,
                    maximum=self.pipeline.settings.MAX_RESULTS,
                    name="limit",
                )
                task_id = body.get("task_id")
                if not task and not task_id:
                    self._json_response({"error": "Missing 'task' field"}, status=400)
                    return
                ctx = self.pipeline.build_context(
                    task=task,
                    project=project,
                    agent=agent,
                    limit=limit,
                    user_id=body.get("user_id"),
                    workspace_id=body.get("workspace_id"),
                    project_id=body.get("project_id") if task_id else None,
                    repository_id=body.get("repository_id"),
                    task_id=task_id,
                    session_id=body.get("session_id"),
                    agent_id=body.get("formal_agent_id") or body.get("agent_id"),
                    known_checkpoint_version=body.get("known_checkpoint_version"),
                )
                self._json_response(
                    _build_context_pack(ctx, task, project, agent, max_tokens)
                )

            else:
                self._json_response({"error": "Not found"}, status=404)

        except APIError as error:
            self._safe_json_response({"error": str(error)}, status=error.status)
        except ContinuityError as error:
            self._safe_json_response(
                {"error": str(error), "code": error.code},
                status=_continuity_status(error),
            )
        except Exception as e:
            if _is_client_disconnect(e):
                logger.debug(
                    f"POST {path} client disconnected before response was sent"
                )
                return
            logger.error(f"POST {path} error: {e}", exc_info=True)
            self._safe_json_response({"error": "Internal server error"}, status=500)

    # ── Helpers ────────────────────────────────────────────────

    def do_DELETE(self):
        path = urlparse(self.path).path.rstrip("/")
        if not self._guard_request("write", path):
            return
        if path.startswith("/memory/") or path.startswith("/memories/"):
            mem_id = path.split("/")[-1]
            if not mem_id:
                self._json_response({"error": "Memory ID is required"}, status=400)
                return
            deleted = self.pipeline.delete(mem_id)
            if not deleted:
                self._json_response({"error": "Memory not found"}, status=404)
                return
            self._json_response({"deleted": True, "id": mem_id})
            return
        self._json_response({"error": "Not found"}, status=404)

    def do_OPTIONS(self):
        origin = self.headers.get("Origin")
        if not origin or origin not in self.pipeline.settings.ALLOWED_ORIGINS:
            self._json_response({"error": "Not found"}, status=404)
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header(
            "Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS"
        )
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Vary", "Origin")
        self.end_headers()

    def _guard_request(self, kind: str, path: str) -> bool:
        settings = self.pipeline.settings
        public_health = {"/health", "/health/live", "/v1/health"}
        if path == "/health/ready":
            client = self.client_address[0] if self.client_address else ""
            if not is_loopback_address(client):
                self._json_response({"error": "Not found"}, status=404)
                return False
            return True
        if path not in public_health:
            authenticator = TokenAuthenticator(settings)
            if not authenticator.validate(self.headers.get("Authorization")):
                self.service_metrics.increment(
                    "recall_auth_failures_total", method=self.command
                )
                self._json_response({"error": "Unauthorized"}, status=401)
                return False

        limiter = type(self).rate_limiter
        if limiter is None:
            limiter = SlidingWindowRateLimiter(
                settings.READS_PER_MINUTE, settings.WRITES_PER_MINUTE
            )
            type(self).rate_limiter = limiter
        client = self.client_address[0] if self.client_address else "unknown"
        if not limiter.allow(client, kind):
            self.service_metrics.increment("recall_rate_limited_total", kind=kind)
            self._json_response({"error": "Rate limit exceeded"}, status=429)
            return False

        self.service_metrics.increment(
            "recall_http_requests_total",
            method=self.command,
            path=path or "/",
        )
        return True

    @staticmethod
    def _bounded_int(
        value: str | int | None,
        *,
        default: int,
        minimum: int,
        maximum: int,
        name: str,
    ) -> int:
        try:
            parsed = default if value is None else int(value)
        except (TypeError, ValueError) as exc:
            raise APIError(f"{name} must be an integer") from exc
        if not minimum <= parsed <= maximum:
            raise APIError(f"{name} must be between {minimum} and {maximum}")
        return parsed

    def _text_response(self, text: str, content_type: str, status: int = 200) -> None:
        encoded = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _json_response(self, data: dict, status: int = 200):
        """Send a bounded JSON response with secure defaults."""
        encoded = json.dumps(data, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        origin = self.headers.get("Origin")
        allowed = self.pipeline.settings.ALLOWED_ORIGINS
        if origin and origin in allowed:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(encoded)

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
        """Read a bounded JSON object from the request."""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError as exc:
            raise APIError("Invalid Content-Length") from exc
        if content_length < 0:
            raise APIError("Invalid Content-Length")
        if content_length > self.pipeline.settings.MAX_BODY_BYTES:
            raise APIError("Request body too large", status=413)
        if content_length == 0:
            return {}
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
        if content_type.lower() != "application/json":
            raise APIError("Content-Type must be application/json", status=415)
        body = self.rfile.read(content_length)
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise APIError("Malformed JSON body") from exc
        if not isinstance(decoded, dict):
            raise APIError("JSON body must be an object")
        return decoded

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


# Deprecated public name retained for integrations importing the former handler.
RemembranceHandler = RecallHandler


def start_rest_api(pipeline: MemoryPipeline, host: str = "127.0.0.1", port: int = 8788):
    """
    Start the REST API server.

    Args:
        pipeline: MemoryPipeline instance
        host: Bind address (default: all interfaces)
        port: Port number (default: 8788, matching Prism's /context/build convention)
    """
    if not is_loopback_address(host) and not pipeline.settings.auth_configured:
        raise ValueError(
            "Refusing to bind Recall externally without bearer authentication"
        )
    token_file = pipeline.settings.API_TOKEN_FILE
    if token_file is not None and not verify_token_file_permissions(token_file):
        raise PermissionError(
            "Recall API token file is missing or has unsafe permissions"
        )
    RecallHandler.pipeline = pipeline
    # Threaded server: a single slow request (e.g. an Ollama-backed capture or
    # a balanced context build) must not block health checks and concurrent
    # readers. SQLite WAL + the default busy timeout handle concurrent access.
    server = ThreadingHTTPServer((host, port), RecallHandler)
    server.daemon_threads = True
    logger.info(f"REST API starting on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("REST API shutting down")
        server.server_close()
