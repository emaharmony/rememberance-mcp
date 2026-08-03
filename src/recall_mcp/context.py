"""Provider-neutral, scope-safe Context Pack V2 application service."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from recall_mcp.continuity import ContinuityError, SessionService, TaskService
from recall_mcp.feedback import RetrievalFeedbackService
from recall_mcp.runtime import Settings
from recall_mcp.store.migrations import CURRENT_SCHEMA_VERSION, run_migrations


CONTEXT_SCHEMA_VERSION = 2
CONTEXT_POLICY_VERSION = "context-v2"
TOKEN_ESTIMATOR_VERSION = "chars-v1"
DEFAULT_BUDGET_WEIGHTS = {
    "continuity": 0.30,
    "retrieval": 0.45,
    "references": 0.15,
    "reserve": 0.10,
}
SUPPORTED_SECTIONS = {
    "active_task",
    "session",
    "decisions",
    "constraints",
    "work_state",
    "retrieval",
    "validation_requests",
}
SUPPORTED_CAPABILITIES = {
    "structured_json",
    "markdown",
    "references",
    "session_delta",
    "expandable_evidence",
}

SearchCallback = Callable[..., tuple[list[dict[str, Any]], str | None]]


def _stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _identifier(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _as_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in ("text", "decision", "constraint", "summary", "title", "question"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return _stable_json(value)


@dataclass(frozen=True)
class ContextPackRequest:
    """Stable input contract shared by local, REST, MCP, and Prism adapters."""

    user_id: str | None = None
    workspace_id: str | None = None
    project_id: str | None = None
    repository_id: str | None = None
    task_id: str | None = None
    session_id: str | None = None
    agent_id: str | None = None
    objective: str | None = None
    max_tokens: int | None = None
    known_checkpoint_version: int | None = None
    known_context_pack_id: str | None = None
    branch: str | None = None
    commit_sha: str | None = None
    requested_sections: tuple[str, ...] = ()
    client_capabilities: tuple[str, ...] = ()
    idempotency_key: str | None = None
    retrieval_limit: int = 10
    schema_version: int = CONTEXT_SCHEMA_VERSION
    include_cold: bool = False
    legacy_project: str | None = None
    legacy_agent: str | None = None
    compatibility_mode: bool = False

    def validated(self, settings: Settings) -> ContextPackRequest:
        if self.schema_version != CONTEXT_SCHEMA_VERSION:
            raise ContinuityError(
                "unsupported context-pack schema version",
                code="unsupported_schema_version",
            )
        required = {
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
            "project_id": self.project_id,
            "repository_id": self.repository_id,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
        }
        if not self.compatibility_mode:
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise ContinuityError(
                    "missing required context scope: " + ", ".join(missing),
                    code="missing_scope",
                )
        if self.known_checkpoint_version is not None:
            if self.known_checkpoint_version < 0:
                raise ContinuityError(
                    "known_checkpoint_version must be zero or greater"
                )
            if not self.session_id:
                raise ContinuityError("known_checkpoint_version requires a session_id")
        budget = self.max_tokens or settings.CONTEXT_DEFAULT_MAX_TOKENS
        if budget <= 0 or budget > settings.CONTEXT_MAX_TOKENS:
            raise ContinuityError(
                f"max_tokens must be between 1 and {settings.CONTEXT_MAX_TOKENS}",
                code="invalid_token_budget",
            )
        if self.retrieval_limit <= 0 or self.retrieval_limit > settings.MAX_RESULTS:
            raise ContinuityError(
                f"retrieval_limit must be between 1 and {settings.MAX_RESULTS}"
            )
        invalid_sections = set(self.requested_sections) - SUPPORTED_SECTIONS
        if invalid_sections:
            raise ContinuityError(
                "unsupported context sections: " + ", ".join(sorted(invalid_sections))
            )
        return self

    def as_dict(self, settings: Settings) -> dict[str, object]:
        result = asdict(self)
        result["max_tokens"] = self.max_tokens or settings.CONTEXT_DEFAULT_MAX_TOKENS
        return {
            key: value for key, value in result.items() if value not in (None, (), "")
        }


@dataclass(frozen=True)
class TokenEstimator:
    """Centralized deterministic approximation for provider-neutral budgeting."""

    version: str = TOKEN_ESTIMATOR_VERSION

    def estimate_text(self, text: str) -> int:
        if not text:
            return 0
        return max(1, math.ceil(len(text.encode("utf-8")) / 4))

    def estimate(self, value: object) -> int:
        return self.estimate_text(
            value if isinstance(value, str) else _stable_json(value)
        )


@dataclass
class _ServiceMetrics:
    built_total: int = 0
    failures_total: int = 0
    latency_seconds_total: float = 0.0
    tokens_estimated_total: int = 0
    budget_exceeded_total: int = 0
    references_total: int = 0
    validation_requests_total: int = 0
    scope_rejections_total: int = 0
    last_error: str = ""


class ContextPackService:
    """Canonical application layer for building and reading Context Pack V2."""

    def __init__(
        self,
        db_path: Path,
        settings: Settings,
        task_service: TaskService,
        session_service: SessionService,
        feedback_service: RetrievalFeedbackService,
        search: SearchCallback | None = None,
    ):
        self.db_path = Path(db_path)
        self.settings = settings
        self.task_service = task_service
        self.session_service = session_service
        self.feedback_service = feedback_service
        self.search = search
        run_migrations(self.db_path)
        self.policy_version = settings.CONTEXT_POLICY_VERSION or CONTEXT_POLICY_VERSION
        self.estimator = TokenEstimator(settings.CONTEXT_TOKEN_ESTIMATOR_VERSION)
        self.budget_weights = {
            **DEFAULT_BUDGET_WEIGHTS,
            **settings.CONTEXT_BUDGET_WEIGHTS,
        }
        self._metrics = _ServiceMetrics()
        self._metrics_lock = threading.Lock()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
        finally:
            conn.close()

    def _record_failure(
        self, exc: BaseException, *, scope_rejection: bool = False
    ) -> None:
        with self._metrics_lock:
            self._metrics.failures_total += 1
            if scope_rejection:
                self._metrics.scope_rejections_total += 1
            self._metrics.last_error = f"{type(exc).__name__}: context build failed"

    def _resolve_scope(
        self, request: ContextPackRequest
    ) -> tuple[dict[str, str | None], dict[str, Any] | None]:
        if request.task_id is None:
            return (
                {
                    "user_id": request.user_id,
                    "workspace_id": request.workspace_id,
                    "project_id": request.project_id,
                    "repository_id": request.repository_id,
                    "task_id": None,
                    "session_id": request.session_id,
                    "agent_id": request.agent_id,
                },
                None,
            )
        task = self.task_service.get_task(
            request.task_id,
            user_id=request.user_id,
            project_id=request.project_id,
            repository_id=request.repository_id,
        )
        if request.workspace_id and task["workspace_id"] != request.workspace_id:
            raise ContinuityError(
                "task is outside the requested scope", code="not_found"
            )
        return (
            {
                "user_id": str(task["user_id"]),
                "workspace_id": str(task["workspace_id"]),
                "project_id": str(task["project_id"]),
                "repository_id": str(task["repository_id"]),
                "task_id": str(task["id"]),
                "session_id": request.session_id,
                "agent_id": request.agent_id,
            },
            task,
        )

    @staticmethod
    def _warning(code: str, message: str, **details: object) -> dict[str, object]:
        return {"code": code, "message": message, **details}

    def _session_state(
        self,
        request: ContextPackRequest,
        scope: Mapping[str, str | None],
        warnings: list[dict[str, object]],
    ) -> tuple[dict[str, object] | None, dict[str, Any]]:
        if not request.session_id:
            return None, {}
        session = self.session_service.get_session(request.session_id)
        if session["task_id"] != scope.get("task_id"):
            raise ContinuityError(
                "session is outside the requested task", code="scope_mismatch"
            )
        checkpoint = session.get("checkpoint") or {}
        version = int(session["version"])
        if request.known_checkpoint_version is None:
            delta: dict[str, object] = {
                "from_version": None,
                "to_version": version,
                "status": "checkpoint",
                "changes": [],
                "latest_checkpoint": checkpoint or None,
            }
        else:
            try:
                raw = self.session_service.get_delta(
                    request.session_id,
                    known_version=request.known_checkpoint_version,
                    agent_id=request.agent_id,
                )
                events = raw.pop("events")
                latest = raw.pop("checkpoint")
                delta = {
                    **raw,
                    "status": "current" if raw["no_change"] else "changed",
                    "changes": [
                        {
                            key: event[key]
                            for key in (
                                "id",
                                "sequence",
                                "event_type",
                                "agent_id",
                                "payload",
                                "created_at",
                            )
                            if key in event
                        }
                        for event in events
                    ],
                    "latest_checkpoint": latest,
                }
            except ContinuityError as exc:
                if exc.code not in {"invalid_version", "version_too_old"}:
                    raise
                warnings.append(
                    self._warning(
                        exc.code,
                        str(exc),
                        requested_version=request.known_checkpoint_version,
                        latest_version=version,
                    )
                )
                delta = {
                    "from_version": request.known_checkpoint_version,
                    "to_version": version,
                    "status": "safe_checkpoint",
                    "changes": [],
                    "latest_checkpoint": checkpoint or None,
                }
        return (
            {
                "id": session["id"],
                "status": session["status"],
                "latest_checkpoint_version": version,
                "checkpoint": checkpoint or None,
                "delta": delta,
                "participants": session.get("participants", []),
            },
            checkpoint,
        )

    @staticmethod
    def _authority_item(
        value: object,
        *,
        authority: str,
        verification: str,
        source: Mapping[str, object],
    ) -> dict[str, object]:
        return {
            "value": value,
            "trust": {"authority": authority, "verification": verification},
            "provenance": dict(source),
        }

    def _checkpoint_sections(
        self, checkpoint: Mapping[str, Any], session_id: str | None
    ) -> tuple[
        dict[str, list[dict[str, object]]],
        dict[str, list[dict[str, object]]],
        dict[str, list[object]],
    ]:
        source = {
            "source_type": "session_checkpoint",
            "session_id": session_id,
            "checkpoint_id": checkpoint.get("id"),
            "checkpoint_version": checkpoint.get("version"),
            "source_agent_id": checkpoint.get("source_agent_id"),
        }
        decisions = {
            "approved": [
                self._authority_item(
                    value,
                    authority="user_approved_decision",
                    verification="approved",
                    source=source,
                )
                for value in checkpoint.get("approved_decisions", [])
            ],
            "proposed": [
                self._authority_item(
                    value,
                    authority="unapproved_recommendation",
                    verification="pending",
                    source=source,
                )
                for value in checkpoint.get("proposed_decisions", [])
            ],
        }
        critical: list[dict[str, object]] = []
        normal: list[dict[str, object]] = []
        for value in checkpoint.get("constraints", []):
            target = normal
            if not isinstance(value, Mapping) or value.get("critical", True):
                target = critical
            elif value.get("priority") == "critical":
                target = critical
            target.append(
                self._authority_item(
                    value,
                    authority="explicit_project_policy",
                    verification="approved",
                    source=source,
                )
            )
        work_state = {
            "completed": list(checkpoint.get("completed", [])),
            "remaining": list(checkpoint.get("remaining", [])),
            "blockers": list(checkpoint.get("blockers", [])),
            "open_questions": list(checkpoint.get("open_questions", [])),
            "important_files": list(checkpoint.get("important_files", [])),
            "known_failures": list(checkpoint.get("known_failures", [])),
        }
        return decisions, {"critical": critical, "normal": normal}, work_state

    def _validation_requests(
        self,
        session_id: str | None,
        proposed: Sequence[Mapping[str, object]],
    ) -> list[dict[str, object]]:
        requests = [
            {
                "id": f"proposed-{index}",
                "question": f"Approve proposed decision: {_as_text(item['value'])}",
                "claims": [item["value"]],
                "evidence": [],
                "pros": [],
                "cons": [],
                "recommendation": item["value"],
                "status": "pending",
                "impact": "high",
                "provenance": item["provenance"],
            }
            for index, item in enumerate(proposed, start=1)
        ]
        if not session_id:
            return requests
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, agent_id, payload_json, created_at
                FROM session_events
                WHERE session_id = ? AND event_type = 'validation.requested'
                ORDER BY sequence
                """,
                (session_id,),
            ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"] or "{}")
            requests.append(
                {
                    "id": row["id"],
                    "question": payload.get("question", "Validation requested"),
                    "claims": payload.get("claims", []),
                    "evidence": payload.get("evidence", []),
                    "pros": payload.get("pros", []),
                    "cons": payload.get("cons", []),
                    "recommendation": payload.get("recommendation"),
                    "status": payload.get("status", "pending"),
                    "impact": payload.get("impact", "normal"),
                    "provenance": {
                        "source_type": "session_event",
                        "event_id": row["id"],
                        "source_agent_id": row["agent_id"],
                        "created_at": row["created_at"],
                    },
                }
            )
        return requests

    def _scoped_memories(
        self,
        results: Sequence[Mapping[str, Any]],
        scope: Mapping[str, str | None],
        *,
        strict: bool,
    ) -> list[dict[str, Any]]:
        if not results:
            return []
        ids = [str(result["id"]) for result in results]
        placeholders = ",".join("?" for _ in ids)
        clauses = [f"m.id IN ({placeholders})"]
        params: list[object] = list(ids)
        if strict:
            for field in (
                "user_id",
                "workspace_id",
                "project_id",
                "repository_id",
                "task_id",
            ):
                clauses.append(f"m.{field} = ?")
                params.append(scope[field])
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT m.*, a.trust_level, a.system_type
                FROM memories AS m
                LEFT JOIN agents AS a ON a.id = m.agent
                WHERE {" AND ".join(clauses)}
                """,
                params,
            ).fetchall()
        by_id = {str(row["id"]): dict(row) for row in rows}
        if strict and len(by_id) != len(set(ids)):
            raise ContinuityError(
                "retrieval returned evidence outside the permitted scope",
                code="scope_mismatch",
            )
        return [{**dict(result), **by_id[str(result["id"])]} for result in results]

    @staticmethod
    def _trust_and_provenance(
        memory: Mapping[str, Any], request: ContextPackRequest
    ) -> tuple[dict[str, object], dict[str, object]]:
        source = str(memory.get("source") or "")
        agent = str(memory.get("agent") or "")
        lower = source.lower()
        if lower.startswith(("repository:", "file:", "git:")):
            authority, verification, source_type = (
                "repository_evidence",
                "verified",
                "repository",
            )
        elif any(marker in lower for marker in ("tool:", "test:", "command:")):
            authority, verification, source_type = (
                "successful_tool_output",
                "verified",
                "tool",
            )
        elif lower.startswith(("http://", "https://", "external:")):
            authority, verification, source_type = (
                "external_content",
                "unverified",
                "external",
            )
        elif agent in {"user", str(memory.get("user_id") or "")}:
            authority, verification, source_type = (
                "user_factual_statement",
                "stated",
                "user",
            )
        elif memory.get("trust_level") in {"trusted", "high"}:
            authority, verification, source_type = (
                "trusted_agent_conclusion",
                "agent_attested",
                "agent",
            )
        else:
            authority, verification, source_type = (
                "unverified_agent_statement",
                "unverified",
                "agent",
            )
        trust = {
            "authority": authority,
            "verification": verification,
            "source_trust_level": memory.get("trust_level") or "standard",
        }
        provenance: dict[str, object] = {
            "source_type": source_type,
            "source_ref": source,
            "memory_id": memory["id"],
            "source_agent_id": agent or None,
            "created_at": memory.get("created_at"),
        }
        if request.branch:
            provenance["branch"] = request.branch
        if request.commit_sha:
            provenance["commit_sha"] = request.commit_sha
        return trust, provenance

    def _allocate(
        self,
        *,
        request: ContextPackRequest,
        active_task: Mapping[str, Any] | None,
        session: Mapping[str, object] | None,
        decisions: Mapping[str, Sequence[Mapping[str, object]]],
        constraints: Mapping[str, Sequence[Mapping[str, object]]],
        work_state: Mapping[str, Sequence[object]],
        validation_requests: Sequence[Mapping[str, object]],
        memories: Sequence[Mapping[str, Any]],
        warnings: list[dict[str, object]],
    ) -> tuple[
        str,
        list[dict[str, object]],
        list[dict[str, object]],
        list[dict[str, object]],
        dict[str, int],
        list[dict[str, object]],
    ]:
        budget = request.max_tokens or self.settings.CONTEXT_DEFAULT_MAX_TOKENS
        mandatory = {
            "active_task": {
                "title": active_task.get("title"),
                "objective": active_task.get("objective"),
                "status": active_task.get("status"),
            }
            if active_task
            else {"objective": request.objective},
            "approved_decisions": list(decisions["approved"]),
            "critical_constraints": list(constraints["critical"]),
            "blockers": list(work_state["blockers"]),
            "session_delta": session.get("delta") if session else None,
            "validation_requests": [
                item for item in validation_requests if item.get("impact") == "high"
            ],
        }
        continuity = {
            "completed": list(work_state["completed"]),
            "remaining": list(work_state["remaining"]),
            "open_questions": list(work_state["open_questions"]),
            "known_failures": list(work_state["known_failures"]),
        }
        mandatory_tokens = self.estimator.estimate(mandatory)
        continuity_limit = int(budget * self.budget_weights["continuity"])
        continuity_tokens = self.estimator.estimate(continuity)
        if continuity_tokens > continuity_limit:
            continuity = {
                key: list(values)[:3] for key, values in continuity.items() if values
            }
            warnings.append(
                self._warning(
                    "continuity_summarized",
                    "Lower-priority continuity state was deterministically compacted.",
                    estimated_original_tokens=continuity_tokens,
                )
            )
            continuity_tokens = self.estimator.estimate(continuity)
        if mandatory_tokens > budget:
            warnings.append(
                self._warning(
                    "mandatory_budget_exceeded",
                    "Mandatory context exceeds the requested token budget; critical content was preserved.",
                    mandatory_tokens=mandatory_tokens,
                    budget=budget,
                )
            )
        reserve = int(budget * self.budget_weights["reserve"])
        remaining = max(0, budget - mandatory_tokens - continuity_tokens - reserve)
        reference_limit = min(
            remaining, int(budget * self.budget_weights["references"])
        )
        retrieval_limit = min(
            max(0, remaining - reference_limit),
            int(budget * self.budget_weights["retrieval"]),
        )
        retrieval_used = 0
        reference_used = 0
        omitted_tokens = 0
        inline_blocks: list[str] = [
            "## Mandatory task context\n" + _stable_json(mandatory),
            "## Continuity\n" + _stable_json(continuity),
        ]
        result_items: list[dict[str, object]] = []
        persistence_items: list[dict[str, object]] = []
        references: list[dict[str, object]] = []
        omissions: list[dict[str, object]] = []
        supports_references = not request.client_capabilities or bool(
            {"references", "expandable_evidence"} & set(request.client_capabilities)
        )
        seen_text: set[str] = set()
        category_counts: dict[str, int] = {}
        for position, memory in enumerate(memories, start=1):
            memory_id = str(memory["id"])
            content = str(memory.get("content") or "")
            summary = str(memory.get("summary") or content)
            text_key = summary.casefold().strip()
            content_tokens = self.estimator.estimate_text(content)
            summary_tokens = self.estimator.estimate_text(summary)
            trust, provenance = self._trust_and_provenance(memory, request)
            reasons = [*memory.get("sources", []), "scope_valid"]
            category = str(memory.get("category") or "memory")
            requires_reference = trust["authority"] in {
                "external_content",
                "unverified_agent_statement",
            } or category in {"hypothesis", "external_claim"}
            if requires_reference:
                reasons.append("verification_required")
            if category_counts.get(category, 0) >= 2:
                requires_reference = True
                reasons.append("diversity_limit")
            disposition = "omitted"
            delivered: str | None = None
            if text_key and text_key in seen_text:
                reasons.append("duplicate")
            elif (
                not requires_reference
                and content_tokens <= self.settings.CONTEXT_INLINE_EVIDENCE_MAX_TOKENS
                and retrieval_used + content_tokens <= retrieval_limit
            ):
                disposition = "inline"
                delivered = content
                retrieval_used += content_tokens
                reasons.append("within_inline_budget")
                seen_text.add(text_key)
            elif (
                not requires_reference
                and summary
                and summary != content
                and summary_tokens <= self.settings.CONTEXT_INLINE_EVIDENCE_MAX_TOKENS
                and retrieval_used + summary_tokens <= retrieval_limit
            ):
                disposition = "summary"
                delivered = summary
                retrieval_used += summary_tokens
                reasons.append("summary_within_budget")
                seen_text.add(text_key)
            else:
                ref_preview = {
                    "type": "memory",
                    "title": summary[:120] or memory_id,
                    "summary": summary[:500],
                }
                reference_tokens = self.estimator.estimate(ref_preview)
                if (
                    supports_references
                    and reference_used + reference_tokens <= reference_limit
                ):
                    disposition = "reference"
                    reference_used += reference_tokens
                    reasons.append("deferred_large_evidence")
                    reference_id = (
                        "ref_"
                        + hashlib.sha256(memory_id.encode("utf-8")).hexdigest()[:24]
                    )
                    references.append(
                        {
                            "reference_id": reference_id,
                            "memory_id": memory_id,
                            "type": "memory",
                            "title": ref_preview["title"],
                            "summary": ref_preview["summary"],
                            "estimated_tokens": content_tokens,
                            "expandable": True,
                            "source": provenance,
                            "trust": trust,
                            "freshness": {"created_at": memory.get("created_at")},
                            "content": {
                                "content": content,
                                "summary": summary,
                                "category": memory.get("category"),
                            },
                        }
                    )
                else:
                    reasons.append("budget_exhausted")
                    omitted_tokens += content_tokens
                    omissions.append(
                        {
                            "type": "memory",
                            "id": memory_id,
                            "estimated_tokens": content_tokens,
                            "reason": "budget_exhausted",
                        }
                    )
            if disposition != "omitted":
                category_counts[category] = category_counts.get(category, 0) + 1
            item: dict[str, object] = {
                "id": memory_id,
                "type": "memory",
                "disposition": disposition,
                "estimated_tokens": content_tokens,
                "trust": trust,
                "provenance": provenance,
                "retrieval": {
                    "rank": memory.get("production_rank", position),
                    "score": memory.get("score"),
                    "keyword_score": memory.get("keyword_score"),
                    "vector_score": memory.get("vector_score"),
                    "graph_score": memory.get("graph_score"),
                    "utility_score": memory.get("utility_score"),
                    "reason": reasons,
                },
            }
            if delivered is not None:
                item["summary"] = delivered
                inline_blocks.append(
                    f"## Evidence {position} ({memory_id})\n{delivered}"
                )
            result_items.append(item)
            persistence_items.append(
                {
                    "memory_id": memory_id,
                    "disposition": disposition,
                    "position": position,
                    "estimated_tokens": content_tokens,
                    "reasons": reasons,
                    "trust": trust,
                    "provenance": provenance,
                }
            )
        estimated_total = (
            mandatory_tokens + continuity_tokens + retrieval_used + reference_used
        )
        token_usage = {
            "budget": budget,
            "estimated_total": estimated_total,
            "mandatory_tokens": mandatory_tokens,
            "continuity_tokens": continuity_tokens,
            "retrieval_tokens": retrieval_used,
            "reference_tokens": reference_used,
            "reserve_tokens": reserve,
            "omitted_tokens": omitted_tokens,
        }
        return (
            "\n\n".join(inline_blocks),
            result_items,
            persistence_items,
            references,
            token_usage,
            omissions,
        )

    def _source_fingerprint(
        self,
        *,
        request: ContextPackRequest,
        task: Mapping[str, Any] | None,
        session: Mapping[str, object] | None,
        checkpoint: Mapping[str, Any],
        memories: Sequence[Mapping[str, Any]],
    ) -> str:
        source = {
            "task": {
                key: task.get(key)
                for key in ("id", "objective", "status", "updated_at")
            }
            if task
            else None,
            "session": {
                "id": session.get("id"),
                "version": session.get("latest_checkpoint_version"),
            }
            if session
            else None,
            "checkpoint": checkpoint,
            "memories": [
                {
                    "id": memory["id"],
                    "created_at": memory.get("created_at"),
                    "content_digest": _digest(
                        [memory.get("summary"), memory.get("content")]
                    ),
                }
                for memory in memories
            ],
            "repository": {"branch": request.branch, "commit_sha": request.commit_sha},
            "policies": {
                "context": self.policy_version,
                "utility": self.feedback_service.policy_version,
                "utility_shadow": self.settings.UTILITY_SHADOW_MODE,
                "estimator": self.estimator.version,
            },
        }
        return "sha256:" + _digest(source)

    def _explanation(
        self,
        *,
        scope: Mapping[str, str | None],
        items: Sequence[Mapping[str, object]],
        token_usage: Mapping[str, int],
        warnings: Sequence[Mapping[str, object]],
        omissions: Sequence[Mapping[str, object]],
    ) -> dict[str, object]:
        return {
            "section_reasons": {
                "active_task": "mandatory active-work state",
                "approved_decisions": "highest-authority decision state",
                "critical_constraints": "protected mandatory policy",
                "session": "compact checkpoint and requested delta",
                "retrieval": "scope-filtered evidence in production rank order",
            },
            "retrieval_items": [
                {
                    "memory_id": item["memory_id"],
                    "disposition": item["disposition"],
                    "reasons": item["reasons"],
                }
                for item in items
            ],
            "scope_filters": {
                key: value
                for key, value in scope.items()
                if key not in {"agent_id"} and value is not None
            },
            "policy_versions": {
                "schema": CONTEXT_SCHEMA_VERSION,
                "context": self.policy_version,
                "token_estimator": self.estimator.version,
                "utility": self.feedback_service.policy_version,
            },
            "token_allocation": dict(token_usage),
            "utility_affected_ranking": bool(
                not self.settings.UTILITY_SHADOW_MODE
                and self.settings.UTILITY_RANKING_WEIGHT > 0
            ),
            "warnings": list(warnings),
            "omissions": list(omissions),
        }

    def _idempotent_replay(
        self,
        request: ContextPackRequest,
        request_digest: str,
    ) -> dict[str, Any] | None:
        if not request.idempotency_key:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM context_packs
                WHERE agent_id IS ? AND idempotency_key = ?
                """,
                (request.agent_id, request.idempotency_key),
            ).fetchone()
        if row is None:
            return None
        if row["request_digest"] != request_digest:
            raise ContinuityError(
                "idempotency key was reused with a different request",
                code="idempotency_conflict",
            )
        pack = json.loads(row["pack_json"])
        pack["idempotent_replay"] = True
        return pack

    def build(self, request: ContextPackRequest) -> dict[str, Any]:
        started = time.perf_counter()
        request = request.validated(self.settings)
        request_body = request.as_dict(self.settings)
        request_digest = _digest(request_body)
        replay = self._idempotent_replay(request, request_digest)
        if replay is not None:
            return replay
        warnings: list[dict[str, object]] = []
        try:
            scope, task = self._resolve_scope(request)
            session, checkpoint = self._session_state(request, scope, warnings)
            decisions, constraints, work_state = self._checkpoint_sections(
                checkpoint, request.session_id
            )
            validation_requests = self._validation_requests(
                request.session_id, decisions["proposed"]
            )
            unknown_capabilities = sorted(
                set(request.client_capabilities) - SUPPORTED_CAPABILITIES
            )
            if unknown_capabilities:
                warnings.append(
                    self._warning(
                        "unsupported_capability",
                        "Unknown client capabilities were ignored.",
                        capabilities=unknown_capabilities,
                    )
                )
            if self.search is None:
                raise ContinuityError(
                    "context retrieval is unavailable", code="service_unavailable"
                )
            objective = (
                request.objective or (task or {}).get("objective") or ""
            ).strip()
            if not objective:
                raise ContinuityError("objective must be a non-empty string")
            results, retrieval_run_id = self.search(
                query=objective,
                category=None,
                tier=None,
                limit=request.retrieval_limit,
                mode="balanced",
                project=request.legacy_project,
                agent=request.legacy_agent,
                user_id=scope["user_id"],
                workspace_id=scope["workspace_id"],
                project_id=scope["project_id"],
                repository_id=scope["repository_id"],
                task_id=scope["task_id"],
                session_id=scope["session_id"],
                agent_id=scope["agent_id"],
                include_cold=request.include_cold,
                retrieval_idempotency_key=None,
            )
            if retrieval_run_id is None:
                raise ContinuityError(
                    "retrieval telemetry could not be persisted",
                    code="telemetry_unavailable",
                )
            memories = self._scoped_memories(
                results, scope, strict=not request.compatibility_mode
            )
            for memory in memories:
                summary = str(memory.get("summary") or memory.get("content") or "")
                category = str(memory.get("category") or "")
                source = str(memory.get("source") or "").lower()
                contradictory = "contradict" in summary.lower()
                if category not in {"hypothesis", "external_claim"} and not (
                    source.startswith(("http://", "https://", "external:"))
                    or contradictory
                ):
                    continue
                trust, provenance = self._trust_and_provenance(memory, request)
                validation_requests.append(
                    {
                        "id": f"validate-{memory['id']}",
                        "question": "Verify retrieved claim before treating it as settled.",
                        "claims": [summary[:500]],
                        "evidence": [provenance],
                        "pros": [],
                        "cons": [],
                        "recommendation": "Validate against current repository evidence.",
                        "status": "pending",
                        "impact": "high" if contradictory else "normal",
                        "trust": trust,
                        "provenance": provenance,
                    }
                )
            if any(
                item.get("impact") == "high" and item.get("status") == "pending"
                for item in validation_requests
            ):
                warnings.append(
                    self._warning(
                        "unresolved_validation",
                        "High-impact unresolved claims are not represented as settled facts.",
                    )
                )
            (
                inline_context,
                retrieval_items,
                persistence_items,
                references,
                token_usage,
                omissions,
            ) = self._allocate(
                request=request,
                active_task=task,
                session=session,
                decisions=decisions,
                constraints=constraints,
                work_state=work_state,
                validation_requests=validation_requests,
                memories=memories,
                warnings=warnings,
            )
            source_fingerprint = self._source_fingerprint(
                request=request,
                task=task,
                session=session,
                checkpoint=checkpoint,
                memories=memories,
            )
            stale_reason = None
            if request.known_context_pack_id:
                previous = self.get(request.known_context_pack_id, scope)
                if previous["freshness"]["source_fingerprint"] != source_fingerprint:
                    stale_reason = "source_changed"
            now = time.time()
            expires_at = now + self.settings.CONTEXT_PACK_TTL_SECONDS
            pack_id = _identifier("context")
            selected_ids = [
                item["memory_id"]
                for item in persistence_items
                if item["disposition"] in {"inline", "summary", "reference"}
            ]
            injected_ids = [
                item["memory_id"]
                for item in persistence_items
                if item["disposition"] in {"inline", "summary"}
            ]
            referenced_ids = [
                item["memory_id"]
                for item in persistence_items
                if item["disposition"] == "reference"
            ]
            omitted_ids = [
                item["memory_id"]
                for item in persistence_items
                if item["disposition"] == "omitted"
            ]
            response: dict[str, Any] = {
                "context_pack_id": pack_id,
                "schema_version": CONTEXT_SCHEMA_VERSION,
                "policy_version": self.policy_version,
                "scope": {
                    **{key: value for key, value in scope.items() if value is not None},
                    **({"branch": request.branch} if request.branch else {}),
                    **(
                        {"commit_sha": request.commit_sha} if request.commit_sha else {}
                    ),
                },
                "request": request_body,
                "active_task": task,
                "session": session,
                "decisions": decisions,
                "constraints": constraints,
                "work_state": work_state,
                "retrieval": {
                    "retrieval_run_id": retrieval_run_id,
                    "results": retrieval_items,
                    "selected_memory_ids": selected_ids,
                    "injected_memory_ids": injected_ids,
                    "referenced_memory_ids": referenced_ids,
                    "omitted_memory_ids": omitted_ids,
                },
                "inline_context": inline_context,
                "references": [
                    {key: value for key, value in reference.items() if key != "content"}
                    for reference in references
                ],
                "validation_requests": validation_requests,
                "warnings": warnings,
                "omissions": omissions,
                "token_usage": token_usage,
                "freshness": {
                    "generated_at": now,
                    "expires_at": expires_at,
                    "source_fingerprint": source_fingerprint,
                    **({"stale_reason": stale_reason} if stale_reason else {}),
                },
            }
            explanation = self._explanation(
                scope=scope,
                items=persistence_items,
                token_usage=token_usage,
                warnings=warnings,
                omissions=omissions,
            )
            latency_ms = (time.perf_counter() - started) * 1000.0
            persisted_id = self.feedback_service.persist_context_pack_v2(
                pack_id=pack_id,
                retrieval_run_id=retrieval_run_id,
                scope=scope,
                request=request_body,
                request_digest=request_digest,
                pack=response,
                explanation=explanation,
                source_fingerprint=source_fingerprint,
                policy_version=self.policy_version,
                token_estimator_version=self.estimator.version,
                token_budget=int(token_usage["budget"]),
                estimated_tokens=int(token_usage["estimated_total"]),
                expires_at=expires_at,
                build_latency_ms=latency_ms,
                idempotency_key=request.idempotency_key,
                items=persistence_items,
                references=references,
            )
            if persisted_id != pack_id:
                return self.get(persisted_id, scope)
            with self._metrics_lock:
                self._metrics.built_total += 1
                self._metrics.latency_seconds_total += latency_ms / 1000.0
                self._metrics.tokens_estimated_total += int(
                    token_usage["estimated_total"]
                )
                self._metrics.references_total += len(references)
                self._metrics.validation_requests_total += len(validation_requests)
                if any(
                    warning["code"] == "mandatory_budget_exceeded"
                    for warning in warnings
                ):
                    self._metrics.budget_exceeded_total += 1
            return response
        except Exception as exc:
            self._record_failure(
                exc,
                scope_rejection=isinstance(exc, ContinuityError)
                and exc.code in {"not_found", "scope_mismatch"},
            )
            raise

    @staticmethod
    def _assert_scope(row: sqlite3.Row, scope: Mapping[str, str | None]) -> None:
        for field in (
            "user_id",
            "workspace_id",
            "project_id",
            "repository_id",
            "task_id",
        ):
            expected = scope.get(field)
            if expected is None or row[field] != expected:
                raise ContinuityError(
                    "context pack was not found in the requested scope",
                    code="not_found",
                )

    def get(
        self, context_pack_id: str, scope: Mapping[str, str | None]
    ) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM context_packs WHERE id = ? AND schema_version = 2",
                (context_pack_id,),
            ).fetchone()
        if row is None:
            raise ContinuityError("context pack was not found", code="not_found")
        self._assert_scope(row, scope)
        return json.loads(row["pack_json"])

    def explain(
        self, context_pack_id: str, scope: Mapping[str, str | None]
    ) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM context_packs WHERE id = ? AND schema_version = 2",
                (context_pack_id,),
            ).fetchone()
        if row is None:
            raise ContinuityError("context pack was not found", code="not_found")
        self._assert_scope(row, scope)
        return {
            "context_pack_id": context_pack_id,
            **json.loads(row["explanation_json"]),
        }

    def expand_reference(
        self,
        context_pack_id: str,
        reference_id: str,
        scope: Mapping[str, str | None],
        *,
        agent_id: str | None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self.get(context_pack_id, scope)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM context_pack_references
                WHERE context_pack_id = ? AND id = ?
                """,
                (context_pack_id, reference_id),
            ).fetchone()
        if row is None:
            raise ContinuityError("context reference was not found", code="not_found")
        memory_id = row["memory_id"]
        if memory_id:
            self.feedback_service.record_context_usage(
                context_pack_id=context_pack_id,
                memory_id=str(memory_id),
                agent_id=agent_id,
                usage_type="expanded",
                metadata={"reference_id": reference_id},
                idempotency_key=idempotency_key
                or f"expand:{reference_id}:{agent_id or 'anonymous'}",
            )
        return {
            "reference_id": reference_id,
            "context_pack_id": context_pack_id,
            "type": row["type"],
            "title": row["title"],
            "summary": row["summary"],
            "estimated_tokens": row["estimated_tokens"],
            "expandable": bool(row["expandable"]),
            "source": json.loads(row["source_json"]),
            "trust": json.loads(row["trust_json"]),
            "freshness": json.loads(row["freshness_json"]),
            **json.loads(row["content_json"]),
        }

    def record_feedback(
        self,
        *,
        context_pack_id: str,
        scope: Mapping[str, str | None],
        agent_id: str | None,
        used_memory_ids: Sequence[str] = (),
        ignored_memory_ids: Sequence[str] = (),
        expanded_memory_ids: Sequence[str] = (),
        corrected_memory_ids: Sequence[str] = (),
        rejected_memory_ids: Sequence[str] = (),
        idempotency_key: str | None = None,
    ) -> list[dict[str, object]]:
        """Scope-check a pack, then delegate feedback to the Phase 2 service."""
        metadata = self.inspect(context_pack_id)
        if metadata["schema_version"] == 2 and metadata["scope_required"]:
            self.get(context_pack_id, scope)
        return self.feedback_service.record_feedback(
            context_pack_id=context_pack_id,
            agent_id=agent_id,
            used_memory_ids=used_memory_ids,
            ignored_memory_ids=ignored_memory_ids,
            expanded_memory_ids=expanded_memory_ids,
            corrected_memory_ids=corrected_memory_ids,
            rejected_memory_ids=rejected_memory_ids,
            idempotency_key=idempotency_key,
        )

    def inspect(self, context_pack_id: str, *, explain: bool = False) -> dict[str, Any]:
        """Return local-admin diagnostics without captured text."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, retrieval_run_id, schema_version, policy_version,
                       token_estimator_version, user_id, workspace_id, project_id,
                       repository_id, task_id, session_id, agent_id, token_budget,
                       estimated_tokens, source_fingerprint, created_at, expires_at,
                       explanation_json, request_json
                FROM context_packs WHERE id = ?
                """,
                (context_pack_id,),
            ).fetchone()
            item_counts = {
                str(item["disposition"]): int(item["count"])
                for item in conn.execute(
                    """
                    SELECT disposition, COUNT(*) AS count FROM context_pack_items
                    WHERE context_pack_id = ? GROUP BY disposition
                    """,
                    (context_pack_id,),
                ).fetchall()
            }
        if row is None:
            raise ContinuityError("context pack was not found", code="not_found")
        result = {
            key: row[key]
            for key in row.keys()
            if key not in {"explanation_json", "request_json"}
        }
        request = json.loads(row["request_json"] or "{}")
        result["scope_required"] = not bool(request.get("compatibility_mode", False))
        result["item_counts"] = item_counts
        if explain:
            result["explanation"] = json.loads(row["explanation_json"])
        return result

    def stats(self) -> dict[str, object]:
        with self._connect() as conn:
            durable_total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM context_packs WHERE schema_version = 2"
                ).fetchone()[0]
            )
            feedback_pending = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM context_packs AS cp
                    WHERE cp.schema_version = 2 AND NOT EXISTS (
                        SELECT 1 FROM context_usage AS cu
                        WHERE cu.context_pack_id = cp.id
                          AND cu.usage_type IN ('used', 'ignored', 'corrected', 'rejected')
                    )
                    """
                ).fetchone()[0]
            )
        with self._metrics_lock:
            return {
                **asdict(self._metrics),
                "durable_total": durable_total,
                "feedback_pending_total": feedback_pending,
                "policy_version": self.policy_version,
                "token_estimator_version": self.estimator.version,
                "schema_version": CONTEXT_SCHEMA_VERSION,
                "migration_available": CURRENT_SCHEMA_VERSION >= 8,
                "service_available": self.search is not None,
            }

    def health(self) -> dict[str, object]:
        stats = self.stats()
        return {
            "available": stats["service_available"],
            "migration_available": stats["migration_available"],
            "feedback_linkage_healthy": True,
            "policy_version": stats["policy_version"],
            "token_estimator_version": stats["token_estimator_version"],
            "last_error": stats["last_error"],
        }
