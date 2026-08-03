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
from urllib.parse import parse_qs, unquote, urlparse
from typing import Mapping, Optional

from recall_mcp.pipeline import MemoryPipeline
from recall_mcp.cag import CAGRequest, ClientState
from recall_mcp.continuity import ContinuityError
from recall_mcp.context import CONTEXT_SCHEMA_VERSION, ContextPackRequest
from recall_mcp.handoff import (
    HandoffCompletion,
    HandoffRequest,
    HandoffScope,
)
from recall_mcp.skills import SkillProposal, SkillScope
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

    markdown = ctx.get("inline_context") or "\n".join(lines).strip()
    references = ctx.get("references", []) or []
    if ctx.get("inline_context") and references:
        reference_lines = ["## Deferred evidence references"]
        for reference in references:
            reference_lines.append(
                f"- {reference.get('title', reference.get('reference_id', 'reference'))}: "
                f"{reference.get('summary', '')}"
            )
        markdown = markdown + "\n\n" + "\n".join(reference_lines)
    selected_ids = list(
        dict.fromkeys(
            [
                *selected_ids,
                *(ctx.get("retrieval") or {}).get("selected_memory_ids", []),
            ]
        )
    )
    token_usage = ctx.get("token_usage") or {}
    token_count = int(token_usage.get("estimated_total", 0))

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
        "retrieval_run_id": ctx.get("retrieval_run_id"),
        "context_pack_id": ctx.get("context_pack_id"),
        "checkpoint_version": (ctx.get("session") or {}).get(
            "latest_checkpoint_version"
        ),
        "validation_requests": ctx.get("validation_requests", []),
        "references": references,
        "source_fingerprint": (ctx.get("freshness") or {}).get("source_fingerprint"),
        "schema_version": ctx.get("schema_version", 1),
    }


class APIError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _continuity_status(error: ContinuityError) -> int:
    if error.code == "not_found":
        return 404
    if error.code == "forbidden":
        return 403
    if error.code in {
        "idempotency_conflict",
        "scope_mismatch",
        "invalid_transition",
        "conflict",
    }:
        return 409
    if error.code in {"service_unavailable", "telemetry_unavailable"}:
        return 503
    return 400


def _scope(values: Mapping[str, object]) -> dict[str, str | None]:
    return {
        key: str(values[key]) if values.get(key) is not None else None
        for key in (
            "user_id",
            "workspace_id",
            "project_id",
            "repository_id",
            "task_id",
        )
    }


def _cag_scope(values: Mapping[str, object]) -> dict[str, str | None]:
    required: dict[str, str | None] = {}
    for field in (
        "user_id",
        "workspace_id",
        "project_id",
        "repository_id",
        "task_id",
        "agent_id",
    ):
        value = values.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ContinuityError(f"missing required CAG scope: {field}")
        required[field] = value.strip()
    session = values.get("session_id")
    required["session_id"] = (
        session.strip() if isinstance(session, str) and session.strip() else None
    )
    return required


def _skill_scope(values: Mapping[str, object]) -> SkillScope:
    required: dict[str, str] = {}
    for field in ("user_id", "workspace_id", "project_id"):
        value = values.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ContinuityError(f"missing required skill scope: {field}")
        required[field] = value.strip()
    repository = values.get("repository_id")
    return SkillScope(
        **required,
        repository_id=(
            repository.strip()
            if isinstance(repository, str) and repository.strip()
            else None
        ),
    )


def _handoff_scope(values: Mapping[str, object]) -> HandoffScope:
    required: dict[str, str] = {}
    for field in (
        "user_id",
        "workspace_id",
        "project_id",
        "repository_id",
        "task_id",
        "session_id",
    ):
        value = values.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ContinuityError(f"missing required handoff scope: {field}")
        required[field] = value.strip()
    return HandoffScope(**required)


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
                probe["retrieval_feedback"] = (
                    self.pipeline.feedback_service.telemetry_stats()
                )
                probe["context_service"] = self.pipeline.context_service.health()
                probe["skill_service"] = self.pipeline.skill_service.health()
                probe["handoff_service"] = self.pipeline.handoff_service.health()
                probe["cag_service"] = self.pipeline.cag_service.health()
                ready = ready and bool(
                    probe["context_service"]["available"]
                    and probe["context_service"]["migration_available"]
                    and probe["context_service"]["feedback_linkage_healthy"]
                )
                ready = ready and bool(
                    probe["skill_service"]["available"]
                    and probe["skill_service"]["migration_current"]
                )
                ready = ready and bool(
                    probe["handoff_service"]["available"]
                    and probe["handoff_service"]["migration_available"]
                )
                ready = ready and bool(
                    probe["cag_service"]["available"]
                    and probe["cag_service"]["migration_available"]
                )
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
                feedback = self.pipeline.feedback_service.telemetry_stats()
                for name in (
                    "retrieval_runs",
                    "retrieval_results",
                    "context_packs",
                    "task_outcomes",
                    "utility_history",
                    "memory_lifecycle_audit",
                ):
                    self.service_metrics.set_gauge(
                        f"recall_{name}", float(str(feedback[name]))
                    )
                self.service_metrics.set_gauge(
                    "recall_retrieval_feedback_errors",
                    float(str(feedback["error_count"])),
                )
                self.service_metrics.set_gauge(
                    "recall_utility_shadow_mode",
                    1.0 if feedback["shadow_mode"] else 0.0,
                )
                self.service_metrics.set_gauge(
                    "recall_utility_ranking_weight",
                    float(str(feedback["ranking_weight"])),
                )
                context_stats = self.pipeline.context_service.stats()
                for metric, field in (
                    ("context_packs_built_total", "built_total"),
                    ("context_pack_failures_total", "failures_total"),
                    (
                        "context_pack_build_latency_seconds",
                        "latency_seconds_total",
                    ),
                    (
                        "context_pack_tokens_estimated_total",
                        "tokens_estimated_total",
                    ),
                    (
                        "context_pack_budget_exceeded_total",
                        "budget_exceeded_total",
                    ),
                    ("context_pack_references_total", "references_total"),
                    (
                        "context_pack_validation_requests_total",
                        "validation_requests_total",
                    ),
                    (
                        "context_pack_scope_rejections_total",
                        "scope_rejections_total",
                    ),
                    (
                        "context_pack_feedback_pending_total",
                        "feedback_pending_total",
                    ),
                ):
                    self.service_metrics.set_gauge(
                        f"recall_{metric}", float(str(context_stats[field]))
                    )
                skill_stats = self.pipeline.skill_service.stats()
                for metric, value in (
                    ("skills_total", skill_stats["total"]),
                    ("skill_versions_total", skill_stats["versions_total"]),
                    (
                        "skill_candidates_total",
                        skill_stats["pending_approval"],
                    ),
                    ("skill_approvals_total", skill_stats["approvals_total"]),
                    ("skill_rejections_total", skill_stats["rejections_total"]),
                    ("skill_stale_total", skill_stats["stale"]),
                    ("skill_refresh_total", skill_stats["refresh_total"]),
                    (
                        "skill_compile_failures_total",
                        skill_stats["compile_failures_total"],
                    ),
                    (
                        "skill_context_injections_total",
                        skill_stats["context_injections_total"],
                    ),
                    (
                        "skill_reference_expansions_total",
                        skill_stats["reference_expansions_total"],
                    ),
                ):
                    self.service_metrics.set_gauge(
                        f"recall_{metric}", float(str(value))
                    )
                handoff_stats = self.pipeline.handoff_service.stats()
                for metric, field in (
                    ("handoffs_created_total", "created_total"),
                    ("handoffs_claimed_total", "claimed_total"),
                    ("handoffs_completed_total", "completed_total"),
                    ("handoffs_blocked_total", "blocked_total"),
                    ("handoffs_rejected_total", "rejected_total"),
                    ("handoffs_expired_total", "expired_total"),
                    ("handoff_build_failures_total", "build_failures_total"),
                    (
                        "handoff_completion_failures_total",
                        "completion_failures_total",
                    ),
                    (
                        "handoff_reference_expansions_total",
                        "reference_expansions_total",
                    ),
                    ("handoff_skill_usage_total", "skill_usage_total"),
                    ("handoff_latency_seconds", "latency_seconds_total"),
                ):
                    self.service_metrics.set_gauge(
                        f"recall_{metric}", float(str(handoff_stats[field]))
                    )
                if self.pipeline.settings.CACHE_METRICS_ENABLED:
                    cag_stats = self.pipeline.cag_service.stats()
                    for metric, field in (
                        ("cag_deliveries_total", "deliveries_total"),
                        ("cag_full_deliveries_total", "full_total"),
                        ("cag_delta_deliveries_total", "delta_total"),
                        ("cag_no_change_total", "no_change_total"),
                        ("cag_refresh_required_total", "refresh_required_total"),
                        ("cag_fallback_total", "fallback_total"),
                        ("cache_hits_total", "cache_hits_total"),
                        ("cache_misses_total", "cache_misses_total"),
                        ("cache_invalidations_total", "cache_invalidations_total"),
                        ("cache_stale_total", "cache_stale_total"),
                        ("cache_corrupt_total", "cache_corrupt_total"),
                        ("skill_delta_deliveries_total", "skill_delta_total"),
                        ("context_delta_deliveries_total", "context_delta_total"),
                        ("handoff_delta_deliveries_total", "handoff_delta_total"),
                        ("estimated_tokens_full_total", "estimated_tokens_full_total"),
                        (
                            "estimated_tokens_delivered_total",
                            "estimated_tokens_delivered_total",
                        ),
                        (
                            "estimated_tokens_avoided_total",
                            "estimated_tokens_avoided_total",
                        ),
                        (
                            "cag_delivery_latency_seconds",
                            "delivery_latency_seconds_total",
                        ),
                        (
                            "cache_lookup_latency_seconds",
                            "cache_lookup_latency_seconds_total",
                        ),
                        (
                            "delta_generation_latency_seconds",
                            "delta_generation_latency_seconds_total",
                        ),
                    ):
                        self.service_metrics.set_gauge(
                            f"recall_{metric}", float(str(cag_stats[field]))
                        )
                    hot_cache = cag_stats["hot_cache"]
                    self.service_metrics.set_gauge(
                        "recall_cache_evictions_total",
                        float(str(hot_cache["evictions"])),
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

            elif path == "/v2/skills":
                values = {
                    key: params.get(key, [None])[0]
                    for key in (
                        "user_id",
                        "workspace_id",
                        "project_id",
                        "repository_id",
                    )
                }
                self._json_response(
                    {
                        "skills": self.pipeline.skill_service.list_skills(
                            scope=_skill_scope(values),
                            status=params.get("status", [None])[0],
                        )
                    }
                )

            elif path.startswith("/v2/skills/"):
                parts = path.split("/")
                skill_id = parts[3]
                values = {
                    key: params.get(key, [None])[0]
                    for key in (
                        "user_id",
                        "workspace_id",
                        "project_id",
                        "repository_id",
                    )
                }
                skill_scope = _skill_scope(values)
                if len(parts) == 5 and parts[4] == "versions":
                    result = {
                        "skill_id": skill_id,
                        "versions": self.pipeline.skill_service.versions(
                            skill_id, scope=skill_scope
                        ),
                    }
                elif len(parts) == 6 and parts[4] == "versions":
                    result = self.pipeline.skill_service.get(
                        skill_id,
                        scope=skill_scope,
                        version=self._bounded_int(
                            parts[5],
                            default=1,
                            minimum=1,
                            maximum=2_147_483_647,
                            name="version",
                        ),
                    )
                elif len(parts) == 5 and parts[4] == "explain":
                    version_value = params.get("version", [None])[0]
                    result = self.pipeline.skill_service.explain(
                        skill_id,
                        scope=skill_scope,
                        version=(int(version_value) if version_value else None),
                    )
                elif len(parts) == 5 and parts[4] == "evidence":
                    version_value = params.get("version", [None])[0]
                    skill = self.pipeline.skill_service.get(
                        skill_id,
                        scope=skill_scope,
                        version=(int(version_value) if version_value else None),
                    )
                    result = {
                        "skill_id": skill_id,
                        "version": skill["version"],
                        "evidence": self.pipeline.skill_service.evidence(
                            skill_id, int(skill["version"]), scope=skill_scope
                        ),
                    }
                elif len(parts) == 4:
                    version_value = params.get("version", [None])[0]
                    result = self.pipeline.skill_service.get(
                        skill_id,
                        scope=skill_scope,
                        version=(int(version_value) if version_value else None),
                    )
                else:
                    raise ContinuityError("skill route was not found", code="not_found")
                self._json_response(result)

            elif path == "/v2/handoffs":
                values = {
                    key: params.get(key, [None])[0]
                    for key in (
                        "user_id",
                        "workspace_id",
                        "project_id",
                        "repository_id",
                        "task_id",
                        "session_id",
                    )
                }
                self._json_response(
                    {
                        "handoffs": self.pipeline.handoff_service.list_handoffs(
                            scope=_handoff_scope(values),
                            agent_id=params.get("agent_id", [None])[0],
                            status=params.get("status", [None])[0],
                        )
                    }
                )

            elif path.startswith("/v2/handoffs/"):
                parts = path.split("/")
                handoff_id = unquote(parts[3])
                values = {
                    key: params.get(key, [None])[0]
                    for key in (
                        "user_id",
                        "workspace_id",
                        "project_id",
                        "repository_id",
                        "task_id",
                        "session_id",
                    )
                }
                handoff_scope = _handoff_scope(values)
                agent_id = params.get("agent_id", [None])[0]
                if len(parts) == 5 and parts[4] == "versions":
                    result = {
                        "handoff_id": handoff_id,
                        "versions": self.pipeline.handoff_service.versions(
                            handoff_id, scope=handoff_scope, agent_id=agent_id
                        ),
                    }
                elif len(parts) == 6 and parts[4] == "versions":
                    result = self.pipeline.handoff_service.get(
                        handoff_id,
                        scope=handoff_scope,
                        version=self._bounded_int(
                            parts[5],
                            default=1,
                            minimum=1,
                            maximum=2_147_483_647,
                            name="version",
                        ),
                        agent_id=agent_id,
                    )
                elif len(parts) == 5 and parts[4] == "delta":
                    result = self.pipeline.handoff_service.get_delta(
                        handoff_id,
                        scope=handoff_scope,
                        known_version=self._bounded_int(
                            params.get("known_version", ["0"])[0],
                            default=0,
                            minimum=0,
                            maximum=2_147_483_647,
                            name="known_version",
                        ),
                        known_checkpoint_version=(
                            int(params["known_checkpoint_version"][0])
                            if params.get("known_checkpoint_version")
                            else None
                        ),
                        agent_id=agent_id,
                    )
                elif len(parts) == 5 and parts[4] == "explain":
                    result = self.pipeline.handoff_service.explain(
                        handoff_id,
                        scope=handoff_scope,
                        version=(
                            int(params["version"][0]) if params.get("version") else None
                        ),
                        agent_id=agent_id,
                    )
                elif len(parts) == 6 and parts[4] == "references":
                    if not agent_id:
                        raise ContinuityError("agent_id is required")
                    result = self.pipeline.handoff_service.expand_reference(
                        handoff_id,
                        parts[5],
                        scope=handoff_scope,
                        agent_id=agent_id,
                    )
                elif len(parts) == 4:
                    result = self.pipeline.handoff_service.get(
                        handoff_id,
                        scope=handoff_scope,
                        version=(
                            int(params["version"][0]) if params.get("version") else None
                        ),
                        agent_id=agent_id,
                    )
                else:
                    raise ContinuityError(
                        "handoff route was not found", code="not_found"
                    )
                self._json_response(result)

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

            elif path == "/v2/cache/status":
                self._json_response(self.pipeline.cag_service.stats())

            elif path.startswith("/v2/cache/entries/"):
                cache_entry_id = unquote(path.split("/")[4])
                cache_scope = _cag_scope(
                    {
                        key: params.get(key, [None])[0]
                        for key in (
                            "user_id",
                            "workspace_id",
                            "project_id",
                            "repository_id",
                            "task_id",
                            "session_id",
                            "agent_id",
                        )
                    }
                )
                self._json_response(
                    self.pipeline.cag_service.inspect_cache_entry(
                        cache_entry_id, cache_scope
                    )
                )

            elif path.startswith("/v2/context/deliveries/"):
                parts = path.split("/")
                delivery_id = unquote(parts[4])
                delivery_scope = _cag_scope(
                    {
                        key: params.get(key, [None])[0]
                        for key in (
                            "user_id",
                            "workspace_id",
                            "project_id",
                            "repository_id",
                            "task_id",
                            "session_id",
                            "agent_id",
                        )
                    }
                )
                if len(parts) == 6 and parts[5] == "explain":
                    result = self.pipeline.cag_service.explain(
                        delivery_id, delivery_scope
                    )
                elif len(parts) == 5:
                    result = self.pipeline.cag_service.get_delivery(
                        delivery_id, delivery_scope
                    )
                else:
                    raise ContinuityError(
                        "delivery route was not found", code="not_found"
                    )
                self._json_response(result)

            elif path.startswith("/v2/context/") and path.endswith("/explain"):
                context_pack_id = path.split("/")[3]
                context_scope = _scope(
                    {
                        key: params.get(key, [None])[0]
                        for key in (
                            "user_id",
                            "workspace_id",
                            "project_id",
                            "repository_id",
                            "task_id",
                        )
                    }
                )
                self._json_response(
                    self.pipeline.context_service.explain(
                        context_pack_id, context_scope
                    )
                )

            elif "/references/" in path and path.startswith("/v2/context/"):
                parts = path.split("/")
                context_pack_id, reference_id = parts[3], parts[5]
                context_scope = _scope(
                    {
                        key: params.get(key, [None])[0]
                        for key in (
                            "user_id",
                            "workspace_id",
                            "project_id",
                            "repository_id",
                            "task_id",
                        )
                    }
                )
                self._json_response(
                    self.pipeline.context_service.expand_reference(
                        context_pack_id,
                        reference_id,
                        context_scope,
                        agent_id=params.get("agent_id", [None])[0],
                        idempotency_key=params.get("idempotency_key", [None])[0],
                    )
                )

            elif path.startswith("/v2/context/") and len(path.split("/")) == 4:
                context_pack_id = path.split("/")[3]
                context_scope = _scope(
                    {
                        key: params.get(key, [None])[0]
                        for key in (
                            "user_id",
                            "workspace_id",
                            "project_id",
                            "repository_id",
                            "task_id",
                        )
                    }
                )
                self._json_response(
                    self.pipeline.context_service.get(context_pack_id, context_scope)
                )

            elif path.startswith("/v2/memories/") and path.endswith("/utility/history"):
                memory_id = path.split("/")[3]
                utility_scope: dict[str, str] = {
                    key: params[key][0]
                    for key in ("user_id", "project_id", "repository_id", "task_id")
                    if key in params
                }
                self._json_response(
                    {
                        "memory_id": memory_id,
                        "history": self.pipeline.feedback_service.utility_history(
                            memory_id, scope=utility_scope
                        ),
                    }
                )

            elif path.startswith("/v2/memories/") and path.endswith("/utility"):
                memory_id = path.split("/")[3]
                utility_scope = {
                    key: params[key][0]
                    for key in ("user_id", "project_id", "repository_id", "task_id")
                    if key in params
                }
                self._json_response(
                    self.pipeline.feedback_service.explain_utility(
                        memory_id, scope=utility_scope
                    )
                )

            elif path == "/v2/admin/utility/shadow-report":
                limit = self._bounded_int(
                    params.get("limit", ["100"])[0],
                    default=100,
                    minimum=1,
                    maximum=1000,
                    name="limit",
                )
                self._json_response(
                    self.pipeline.feedback_service.shadow_report(limit=limit)
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

                results = self.pipeline.search(
                    query=query,
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
                    agent_id=params.get("agent_id", [None])[0],
                    include_cold=params.get("include_cold", ["false"])[0].lower()
                    in {"1", "true", "yes"},
                    retrieval_idempotency_key=params.get("idempotency_key", [None])[0],
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
                    token_budget=(
                        self._bounded_int(
                            params["token_budget"][0],
                            default=2500,
                            minimum=1,
                            maximum=100_000,
                            name="token_budget",
                        )
                        if "token_budget" in params
                        else None
                    ),
                    include_cold=params.get("include_cold", ["false"])[0].lower()
                    in {"1", "true", "yes"},
                    retrieval_idempotency_key=params.get("idempotency_key", [None])[0],
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

            elif path == "/v2/skills/propose":
                sources = body.get("sources", [])
                if not isinstance(sources, list) or not all(
                    isinstance(item, dict) for item in sources
                ):
                    raise APIError("sources must be an array of objects")
                result = self.pipeline.skill_service.propose(
                    SkillProposal(
                        scope=_skill_scope(body),
                        slug=_required_text(body, "slug"),
                        title=_required_text(body, "title"),
                        purpose=_required_text(body, "purpose"),
                        created_by_agent_id=_required_text(
                            body, "created_by_agent_id", alias="agent_id"
                        ),
                        sources=tuple(sources),
                        summary=str(body.get("summary") or ""),
                        instructions=tuple(body.get("instructions") or ()),
                        facts=tuple(body.get("facts") or ()),
                        decisions=tuple(body.get("decisions") or ()),
                        constraints=tuple(body.get("constraints") or ()),
                        open_questions=tuple(body.get("open_questions") or ()),
                        expires_at=body.get("expires_at"),
                        retention_review_at=body.get("retention_review_at"),
                        idempotency_key=body.get("idempotency_key"),
                        compiler_model=body.get("compiler_model"),
                        generated=bool(body.get("generated", False)),
                    )
                )
                self._json_response(result, status=201)

            elif path.startswith("/v2/skills/"):
                parts = path.split("/")
                skill_id = parts[3]
                skill_scope = _skill_scope(body)
                if (
                    len(parts) == 7
                    and parts[4] == "versions"
                    and parts[6] in {"approve", "reject"}
                ):
                    version = self._bounded_int(
                        parts[5],
                        default=1,
                        minimum=1,
                        maximum=2_147_483_647,
                        name="version",
                    )
                    if parts[6] == "approve":
                        result = self.pipeline.skill_service.approve(
                            skill_id,
                            version,
                            scope=skill_scope,
                            reviewer_id=_required_text(body, "reviewer_id"),
                            reason=str(body.get("reason") or ""),
                            idempotency_key=body.get("idempotency_key"),
                        )
                    else:
                        result = self.pipeline.skill_service.reject(
                            skill_id,
                            version,
                            scope=skill_scope,
                            reviewer_id=_required_text(body, "reviewer_id"),
                            reason=_required_text(body, "reason"),
                            idempotency_key=body.get("idempotency_key"),
                        )
                elif len(parts) == 5 and parts[4] == "refresh":
                    overrides = body.get("sources")
                    if overrides is not None and (
                        not isinstance(overrides, list)
                        or not all(isinstance(item, dict) for item in overrides)
                    ):
                        raise APIError("sources must be an array of objects")
                    result = self.pipeline.skill_service.refresh(
                        skill_id,
                        scope=skill_scope,
                        created_by_agent_id=_required_text(
                            body, "created_by_agent_id", alias="agent_id"
                        ),
                        source_overrides=overrides,
                    )
                elif len(parts) == 5 and parts[4] == "feedback":
                    result = self.pipeline.skill_service.record_usage(
                        skill_id,
                        int(body.get("version", 0)),
                        scope=skill_scope,
                        usage_type=_required_text(body, "usage_type"),
                        agent_id=body.get("agent_id"),
                        context_pack_id=body.get("context_pack_id"),
                        task_id=body.get("task_id"),
                        session_id=body.get("session_id"),
                        metadata=body.get("metadata"),
                        idempotency_key=body.get("idempotency_key"),
                    )
                else:
                    raise ContinuityError("skill route was not found", code="not_found")
                self._json_response(result)

            elif path == "/v2/handoffs":
                capabilities = body.get("capabilities", [])
                if not isinstance(capabilities, list) or not all(
                    isinstance(value, str) for value in capabilities
                ):
                    raise APIError("capabilities must be an array of strings")
                result = self.pipeline.handoff_service.create(
                    HandoffRequest(
                        scope=_handoff_scope(body),
                        source_agent_id=_required_text(body, "source_agent_id"),
                        target_agent_id=_required_text(body, "target_agent_id"),
                        requested_by=_required_text(body, "requested_by"),
                        expected_output=_required_text(body, "expected_output"),
                        known_checkpoint_version=int(
                            body.get("known_checkpoint_version", 0)
                        ),
                        max_tokens=body.get("max_tokens"),
                        capabilities=tuple(capabilities),
                        idempotency_key=body.get("idempotency_key"),
                        status=str(body.get("status", "ready")),
                    )
                )
                self._json_response(result, status=201)

            elif path.startswith("/v2/handoffs/"):
                parts = path.split("/")
                handoff_id = unquote(parts[3])
                action = parts[4] if len(parts) == 5 else ""
                handoff_scope = _handoff_scope(body)
                agent_id = _required_text(body, "agent_id")
                if action == "claim":
                    result = self.pipeline.handoff_service.claim(
                        handoff_id,
                        scope=handoff_scope,
                        agent_id=agent_id,
                        idempotency_key=body.get("idempotency_key"),
                    )
                elif action == "progress":
                    progress = body.get("progress", {})
                    if not isinstance(progress, dict):
                        raise APIError("progress must be an object")
                    result = self.pipeline.handoff_service.progress(
                        handoff_id,
                        scope=handoff_scope,
                        agent_id=agent_id,
                        progress=progress,
                        idempotency_key=body.get("idempotency_key"),
                    )
                elif action == "block":
                    blocker = body.get("blocker", {})
                    if not isinstance(blocker, dict):
                        raise APIError("blocker must be an object")
                    result = self.pipeline.handoff_service.block(
                        handoff_id,
                        scope=handoff_scope,
                        agent_id=agent_id,
                        blocker=blocker,
                        idempotency_key=body.get("idempotency_key"),
                    )
                elif action == "complete":
                    tests = body.get("tests", {})
                    if not isinstance(tests, dict):
                        raise APIError("tests must be an object")
                    result = self.pipeline.handoff_service.complete(
                        handoff_id,
                        scope=handoff_scope,
                        agent_id=agent_id,
                        completion=HandoffCompletion(
                            files_changed=tuple(body.get("files_changed", [])),
                            work_completed=tuple(body.get("work_completed", [])),
                            tests=tests,
                            blockers=tuple(body.get("blockers", [])),
                            remaining_work=tuple(body.get("remaining_work", [])),
                            new_decisions=tuple(body.get("new_decisions", [])),
                            new_questions=tuple(body.get("new_questions", [])),
                            used_skill_versions=tuple(
                                body.get("used_skill_versions", [])
                            ),
                            used_memory_ids=tuple(body.get("used_memory_ids", [])),
                            expanded_references=tuple(
                                body.get("expanded_references", [])
                            ),
                        ),
                        idempotency_key=body.get("idempotency_key"),
                    )
                elif action == "cancel":
                    result = self.pipeline.handoff_service.cancel(
                        handoff_id,
                        scope=handoff_scope,
                        agent_id=agent_id,
                        reason=_required_text(body, "reason"),
                        idempotency_key=body.get("idempotency_key"),
                    )
                elif action == "reject":
                    result = self.pipeline.handoff_service.reject(
                        handoff_id,
                        scope=handoff_scope,
                        agent_id=agent_id,
                        reason=_required_text(body, "reason"),
                        idempotency_key=body.get("idempotency_key"),
                    )
                else:
                    raise ContinuityError(
                        "handoff route was not found", code="not_found"
                    )
                self._json_response(result)

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

            elif path == "/v2/context/deliver":
                requested_sections = body.get("requested_sections", [])
                context_capabilities = body.get("client_capabilities", [])
                if not isinstance(requested_sections, list) or not all(
                    isinstance(value, str) for value in requested_sections
                ):
                    raise APIError("requested_sections must be an array of strings")
                if not isinstance(context_capabilities, list) or not all(
                    isinstance(value, str) for value in context_capabilities
                ):
                    raise APIError("client_capabilities must be an array of strings")
                state_body = body.get("client_state")
                client_state = None
                if state_body is not None:
                    if not isinstance(state_body, dict):
                        raise APIError("client_state must be an object")
                    state_capabilities = state_body.get("capabilities", [])
                    known_skills = state_body.get("known_skills", {})
                    known_handoffs = state_body.get("known_handoffs", {})
                    if not isinstance(state_capabilities, list) or not all(
                        isinstance(value, str) for value in state_capabilities
                    ):
                        raise APIError("client_state.capabilities must be strings")
                    if not isinstance(known_skills, dict) or not isinstance(
                        known_handoffs, dict
                    ):
                        raise APIError(
                            "known skill and handoff versions must be objects"
                        )
                    client_state = ClientState(
                        client_id=state_body.get("client_id"),
                        client_type=state_body.get("client_type"),
                        known_checkpoint_version=state_body.get(
                            "known_checkpoint_version"
                        ),
                        known_context_pack_id=state_body.get("known_context_pack_id"),
                        known_context_pack_fingerprint=state_body.get(
                            "known_context_pack_fingerprint"
                        ),
                        known_skills=known_skills,
                        known_handoffs=known_handoffs,
                        capabilities=tuple(state_capabilities),
                    )
                result = self.pipeline.deliver_context(
                    CAGRequest(
                        context=ContextPackRequest(
                            user_id=body.get("user_id"),
                            workspace_id=body.get("workspace_id"),
                            project_id=body.get("project_id"),
                            repository_id=body.get("repository_id"),
                            task_id=body.get("task_id"),
                            session_id=body.get("session_id"),
                            agent_id=body.get("agent_id"),
                            objective=body.get("objective"),
                            max_tokens=self._bounded_int(
                                body.get("max_tokens"),
                                default=self.pipeline.settings.CONTEXT_DEFAULT_MAX_TOKENS,
                                minimum=1,
                                maximum=self.pipeline.settings.CONTEXT_MAX_TOKENS,
                                name="max_tokens",
                            ),
                            branch=body.get("branch"),
                            commit_sha=body.get("commit_sha"),
                            requested_sections=tuple(requested_sections),
                            client_capabilities=tuple(context_capabilities),
                            retrieval_limit=self._bounded_int(
                                body.get("retrieval_limit", body.get("limit")),
                                default=10,
                                minimum=1,
                                maximum=self.pipeline.settings.MAX_RESULTS,
                                name="retrieval_limit",
                            ),
                            include_cold=bool(body.get("include_cold", False)),
                        ),
                        client_state=client_state,
                        idempotency_key=body.get("idempotency_key"),
                    )
                )
                self._json_response(result, status=201)

            elif path == "/v2/cache/invalidate":
                result = self.pipeline.cag_service.invalidate(
                    scope=_cag_scope(body),
                    cache_entry_id=body.get("cache_entry_id"),
                    reason=str(body.get("reason") or "manual_invalidation"),
                    actor_id=body.get("actor_id", body.get("agent_id")),
                )
                self._json_response(result)

            elif path.startswith("/v2/context/deliveries/") and path.endswith(
                "/feedback"
            ):
                delivery_id = unquote(path.split("/")[4])
                result = self.pipeline.cag_service.record_feedback(
                    delivery_id,
                    scope=_cag_scope(body),
                    agent_id=_required_text(body, "agent_id"),
                    used_skill_versions=body.get("used_skill_versions", []),
                    used_context_sections=body.get("used_context_sections", []),
                    expanded_references=body.get("expanded_references", []),
                    outcome=body.get("outcome"),
                    idempotency_key=body.get("idempotency_key"),
                )
                self._json_response({"feedback": result}, status=201)

            elif path == "/v2/context/build":
                requested_sections = body.get("requested_sections", [])
                capabilities = body.get(
                    "client_capabilities", body.get("capabilities", [])
                )
                if not isinstance(requested_sections, list) or not all(
                    isinstance(value, str) for value in requested_sections
                ):
                    raise APIError("requested_sections must be an array of strings")
                if not isinstance(capabilities, list) or not all(
                    isinstance(value, str) for value in capabilities
                ):
                    raise APIError("client_capabilities must be an array of strings")
                result = self.pipeline.build_context_v2(
                    ContextPackRequest(
                        user_id=body.get("user_id"),
                        workspace_id=body.get("workspace_id"),
                        project_id=body.get("project_id"),
                        repository_id=body.get("repository_id"),
                        task_id=body.get("task_id"),
                        session_id=body.get("session_id"),
                        agent_id=body.get("agent_id"),
                        objective=body.get("objective"),
                        max_tokens=self._bounded_int(
                            body.get("max_tokens"),
                            default=self.pipeline.settings.CONTEXT_DEFAULT_MAX_TOKENS,
                            minimum=1,
                            maximum=self.pipeline.settings.CONTEXT_MAX_TOKENS,
                            name="max_tokens",
                        ),
                        known_checkpoint_version=body.get("known_checkpoint_version"),
                        known_context_pack_id=body.get("known_context_pack_id"),
                        branch=body.get("branch"),
                        commit_sha=body.get("commit_sha"),
                        requested_sections=tuple(requested_sections),
                        client_capabilities=tuple(capabilities),
                        idempotency_key=body.get("idempotency_key"),
                        retrieval_limit=self._bounded_int(
                            body.get("retrieval_limit", body.get("limit")),
                            default=10,
                            minimum=1,
                            maximum=self.pipeline.settings.MAX_RESULTS,
                            name="retrieval_limit",
                        ),
                        schema_version=int(
                            body.get("schema_version", CONTEXT_SCHEMA_VERSION)
                        ),
                        include_cold=bool(body.get("include_cold", False)),
                    )
                )
                self._json_response(result, status=201)

            elif path.startswith("/v2/context/") and path.endswith("/feedback"):
                context_pack_id = path.split("/")[3]
                feedback_result = self.pipeline.context_service.record_feedback(
                    context_pack_id=context_pack_id,
                    scope=_scope(body),
                    agent_id=body.get("agent_id"),
                    used_memory_ids=body.get("used_memory_ids", []),
                    ignored_memory_ids=body.get("ignored_memory_ids", []),
                    expanded_memory_ids=body.get("expanded_memory_ids", []),
                    corrected_memory_ids=body.get("corrected_memory_ids", []),
                    rejected_memory_ids=body.get("rejected_memory_ids", []),
                    used_skill_versions=body.get("used_skill_versions", []),
                    ignored_skill_versions=body.get("ignored_skill_versions", []),
                    corrected_skill_versions=body.get("corrected_skill_versions", []),
                    rejected_skill_versions=body.get("rejected_skill_versions", []),
                    idempotency_key=body.get("idempotency_key"),
                )
                self._json_response({"feedback": feedback_result}, status=201)

            elif path.startswith("/v2/retrieval/") and path.endswith("/feedback"):
                retrieval_run_id = path.split("/")[3]
                context_pack_id = self.pipeline.feedback_service.context_pack_for_run(
                    retrieval_run_id
                )
                feedback_result = self.pipeline.feedback_service.record_feedback(
                    context_pack_id=context_pack_id,
                    agent_id=body.get("agent_id"),
                    used_memory_ids=body.get("used_memory_ids", []),
                    ignored_memory_ids=body.get("ignored_memory_ids", []),
                    expanded_memory_ids=body.get("expanded_memory_ids", []),
                    corrected_memory_ids=body.get("corrected_memory_ids", []),
                    rejected_memory_ids=body.get("rejected_memory_ids", []),
                    idempotency_key=body.get("idempotency_key"),
                )
                self._json_response({"feedback": feedback_result}, status=201)

            elif path.startswith("/v2/tasks/") and path.endswith("/outcome"):
                outcome_task_id = path.split("/")[3]
                outcome_result = self.pipeline.feedback_service.record_task_outcome(
                    task_id=outcome_task_id,
                    session_id=body.get("session_id"),
                    status=body.get("status", "completed"),
                    successful=bool(body.get("successful", False)),
                    agent_id=body.get("agent_id"),
                    user_correction_count=int(body.get("user_correction_count", 0)),
                    rework_required=bool(body.get("rework_required", False)),
                    metadata=body.get("metadata"),
                    idempotency_key=body.get("idempotency_key"),
                )
                self._json_response(outcome_result, status=201)

            elif path.startswith("/v2/memories/") and "/utility/" in path:
                parts = path.split("/")
                memory_id = parts[3]
                action = parts[5]
                actor_id = body.get("actor_id")
                if action == "cold":
                    lifecycle_result = self.pipeline.feedback_service.demote_to_cold(
                        memory_id,
                        reason=body.get("reason", "manual"),
                        actor_id=actor_id,
                    )
                elif action == "restore":
                    lifecycle_result = self.pipeline.feedback_service.restore_from_cold(
                        memory_id, actor_id=actor_id
                    )
                elif action == "pin":
                    lifecycle_result = self.pipeline.feedback_service.pin(
                        memory_id, actor_id=actor_id
                    )
                elif action == "unpin":
                    lifecycle_result = self.pipeline.feedback_service.unpin(
                        memory_id, actor_id=actor_id
                    )
                else:
                    raise ContinuityError("unsupported utility action")
                self._json_response(lifecycle_result)

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
                context_task_id = body.get("task_id")
                limit = self._bounded_int(
                    body.get("limit"),
                    default=10,
                    minimum=1,
                    maximum=self.pipeline.settings.MAX_RESULTS,
                    name="limit",
                )
                if not task and not context_task_id:
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
                    task_id=context_task_id,
                    session_id=body.get("session_id"),
                    agent_id=body.get("agent_id"),
                    known_checkpoint_version=body.get("known_checkpoint_version"),
                    token_budget=body.get("token_budget"),
                    include_cold=bool(body.get("include_cold", False)),
                    retrieval_idempotency_key=body.get("idempotency_key"),
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
                prism_task_id = body.get("task_id")
                if not task and not prism_task_id:
                    self._json_response({"error": "Missing 'task' field"}, status=400)
                    return
                ctx = self.pipeline.build_context(
                    task=task,
                    project=project,
                    agent=agent,
                    limit=limit,
                    user_id=body.get("user_id"),
                    workspace_id=body.get("workspace_id"),
                    project_id=body.get("project_id") if prism_task_id else None,
                    repository_id=body.get("repository_id"),
                    task_id=prism_task_id,
                    session_id=body.get("session_id"),
                    agent_id=body.get("formal_agent_id") or body.get("agent_id"),
                    known_checkpoint_version=body.get("known_checkpoint_version"),
                    token_budget=max_tokens,
                    include_cold=bool(body.get("include_cold", False)),
                    retrieval_idempotency_key=body.get("idempotency_key"),
                )
                response = _build_context_pack(ctx, task, project, agent, max_tokens)
                self._json_response(response)

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
