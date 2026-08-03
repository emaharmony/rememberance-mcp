"""Provider-neutral, immutable cross-agent handoff application service."""

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
from typing import Any, Iterator, Mapping, Sequence

from recall_mcp.context import ContextPackRequest, ContextPackService
from recall_mcp.continuity import ContinuityError, SessionService, TaskService
from recall_mcp.runtime import Settings
from recall_mcp.skills import SkillScope, SkillService
from recall_mcp.store.migrations import CURRENT_SCHEMA_VERSION, run_migrations


HANDOFF_SCHEMA_VERSION = 1
HANDOFF_POLICY_VERSION = "handoff-v1"
HANDOFF_STATUSES = {
    "draft",
    "ready",
    "claimed",
    "in_progress",
    "completed",
    "blocked",
    "rejected",
    "expired",
    "cancelled",
    "superseded",
}
TERMINAL_STATUSES = {"completed", "rejected", "expired", "cancelled", "superseded"}
TRANSITIONS = {
    "draft": {"ready", "cancelled"},
    "ready": {"claimed", "rejected", "expired", "cancelled", "superseded"},
    "claimed": {"in_progress", "blocked", "completed", "cancelled", "expired"},
    "in_progress": {"blocked", "completed", "cancelled", "expired"},
    "blocked": {"in_progress", "completed", "cancelled", "expired"},
}


def _stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _identifier(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _estimate(value: object) -> int:
    text = value if isinstance(value, str) else _stable_json(value)
    return 0 if not text else max(1, math.ceil(len(text.encode("utf-8")) / 4))


def _list(name: str, value: Sequence[object] | None) -> list[object]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        raise ContinuityError(f"{name} must be an array")
    return list(value)


@dataclass(frozen=True)
class HandoffScope:
    user_id: str
    workspace_id: str
    project_id: str
    repository_id: str
    task_id: str
    session_id: str

    def validated(self) -> HandoffScope:
        for name, value in asdict(self).items():
            if not isinstance(value, str) or not value.strip():
                raise ContinuityError(f"{name} must be a non-empty string")
        return self

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class HandoffRequest:
    scope: HandoffScope
    source_agent_id: str
    target_agent_id: str
    requested_by: str
    expected_output: str
    known_checkpoint_version: int = 0
    max_tokens: int | None = None
    capabilities: tuple[str, ...] = ()
    idempotency_key: str | None = None
    status: str = "ready"

    def validated(self, settings: Settings) -> HandoffRequest:
        self.scope.validated()
        for name in (
            "source_agent_id",
            "target_agent_id",
            "requested_by",
            "expected_output",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContinuityError(f"{name} must be a non-empty string")
        if self.source_agent_id == self.target_agent_id:
            raise ContinuityError("source and target agents must differ")
        if self.known_checkpoint_version < 0:
            raise ContinuityError("known_checkpoint_version must be zero or greater")
        if self.status not in {"draft", "ready"}:
            raise ContinuityError("new handoffs must be draft or ready")
        budget = self.max_tokens or settings.CONTEXT_DEFAULT_MAX_TOKENS
        if budget <= 0 or budget > settings.CONTEXT_MAX_TOKENS:
            raise ContinuityError("invalid handoff token budget")
        return self


@dataclass(frozen=True)
class HandoffCompletion:
    files_changed: tuple[object, ...] = ()
    work_completed: tuple[object, ...] = ()
    tests: Mapping[str, object] | None = None
    blockers: tuple[object, ...] = ()
    remaining_work: tuple[object, ...] = ()
    new_decisions: tuple[object, ...] = ()
    new_questions: tuple[object, ...] = ()
    used_skill_versions: tuple[str, ...] = ()
    used_memory_ids: tuple[str, ...] = ()
    expanded_references: tuple[str, ...] = ()


@dataclass
class _Metrics:
    created_total: int = 0
    claimed_total: int = 0
    completed_total: int = 0
    blocked_total: int = 0
    rejected_total: int = 0
    expired_total: int = 0
    build_failures_total: int = 0
    completion_failures_total: int = 0
    reference_expansions_total: int = 0
    skill_usage_total: int = 0
    latency_seconds_total: float = 0.0
    last_error: str = ""


class HandoffService:
    """Build, authorize, transition, and complete single-target handoffs."""

    def __init__(
        self,
        db_path: Path,
        settings: Settings,
        task_service: TaskService,
        session_service: SessionService,
        context_service: ContextPackService,
        skill_service: SkillService,
    ):
        self.db_path = Path(db_path)
        self.settings = settings
        self.task_service = task_service
        self.session_service = session_service
        self.context_service = context_service
        self.skill_service = skill_service
        self.policy_version = settings.HANDOFF_POLICY_VERSION or HANDOFF_POLICY_VERSION
        run_migrations(self.db_path)
        self._metrics = _Metrics()
        self._metrics_lock = threading.Lock()

    @contextmanager
    def _connect(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            if immediate:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _scope_predicate() -> str:
        return (
            "user_id = ? AND workspace_id = ? AND project_id = ? "
            "AND repository_id = ? AND task_id = ? AND session_id = ?"
        )

    @staticmethod
    def _scope_values(scope: HandoffScope) -> tuple[str, ...]:
        return (
            scope.user_id,
            scope.workspace_id,
            scope.project_id,
            scope.repository_id,
            scope.task_id,
            scope.session_id,
        )

    def _resolve_scope(
        self, scope: HandoffScope
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        scope.validated()
        task = self.task_service.get_task(
            scope.task_id,
            user_id=scope.user_id,
            project_id=scope.project_id,
            repository_id=scope.repository_id,
        )
        if task["workspace_id"] != scope.workspace_id:
            raise ContinuityError(
                "task is outside the requested scope", code="not_found"
            )
        session = self.session_service.get_session(scope.session_id)
        if session["task_id"] != scope.task_id:
            raise ContinuityError(
                "session is outside the requested task", code="scope_mismatch"
            )
        return task, session

    @staticmethod
    def _participant(session: Mapping[str, Any], agent_id: str) -> bool:
        return any(item["agent_id"] == agent_id for item in session["participants"])

    @staticmethod
    def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    def _version_dict(
        self, conn: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        handoff = conn.execute(
            "SELECT * FROM handoffs WHERE id = ?", (row["handoff_id"],)
        ).fetchone()
        assert handoff is not None
        result = json.loads(row["content_json"])
        result.update(
            {
                "status": handoff["status"],
                "current_version": int(handoff["current_version"]),
                "version_status": (
                    "superseded"
                    if int(row["version"]) < int(handoff["current_version"])
                    else handoff["status"]
                ),
                "content_markdown": row["content_markdown"],
                "compact_markdown": row["compact_markdown"],
                "content_hash": row["content_hash"],
                "source_fingerprint": row["source_fingerprint"],
                "context_pack_id": row["context_pack_id"],
                "token_estimate": int(row["token_estimate"]),
                "policy_version": row["policy_version"],
                "created_at": row["created_at"],
                "expires_at": handoff["expires_at"],
            }
        )
        return result

    def _authorized_row(
        self,
        conn: sqlite3.Connection,
        handoff_id: str,
        *,
        scope: HandoffScope,
        agent_id: str | None = None,
    ) -> sqlite3.Row:
        row = conn.execute(
            f"SELECT * FROM handoffs WHERE id = ? AND {self._scope_predicate()}",
            (handoff_id, *self._scope_values(scope)),
        ).fetchone()
        if row is None:
            raise ContinuityError(
                "handoff was not found in the requested scope", code="not_found"
            )
        if agent_id and agent_id not in {
            row["source_agent_id"],
            row["target_agent_id"],
            row["requested_by"],
        }:
            raise ContinuityError(
                "agent is not authorized for this handoff", code="not_found"
            )
        return row

    def _event(
        self,
        conn: sqlite3.Connection,
        handoff: sqlite3.Row,
        *,
        event_type: str,
        agent_id: str,
        payload: Mapping[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if idempotency_key:
            prior = conn.execute(
                "SELECT * FROM handoff_events WHERE handoff_id = ? AND idempotency_key = ?",
                (handoff["id"], idempotency_key),
            ).fetchone()
            if prior:
                if prior["event_type"] != event_type or json.loads(
                    prior["payload_json"]
                ) != dict(payload or {}):
                    raise ContinuityError(
                        "idempotency key was reused with different event input",
                        code="idempotency_conflict",
                    )
                result = dict(prior)
                result["payload"] = json.loads(result.pop("payload_json"))
                result["idempotent_replay"] = True
                return result
        event_id = _identifier("handoff_event")
        now = time.time()
        conn.execute(
            """
            INSERT INTO handoff_events (
                id, handoff_id, version, agent_id, event_type,
                payload_json, idempotency_key, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                handoff["id"],
                int(handoff["current_version"]),
                agent_id,
                event_type,
                _stable_json(dict(payload or {})),
                idempotency_key,
                now,
            ),
        )
        return {
            "id": event_id,
            "handoff_id": handoff["id"],
            "version": int(handoff["current_version"]),
            "agent_id": agent_id,
            "event_type": event_type,
            "payload": dict(payload or {}),
            "created_at": now,
            "idempotent_replay": False,
        }

    @staticmethod
    def _state_stale(
        conn: sqlite3.Connection,
        handoff: sqlite3.Row,
        task: Mapping[str, Any],
        session: Mapping[str, Any],
    ) -> bool:
        if int(handoff["source_checkpoint_version"]) != int(session["version"]):
            return True
        version = conn.execute(
            """
            SELECT content_json FROM handoff_versions
            WHERE handoff_id = ? AND version = ?
            """,
            (handoff["id"], int(handoff["current_version"])),
        ).fetchone()
        if version is None:
            return False
        content = json.loads(version["content_json"])
        source_updated_at = content.get("task", {}).get("source_updated_at")
        return source_updated_at is not None and float(source_updated_at) != float(
            task["updated_at"]
        )

    @staticmethod
    def _render_markdown(content: Mapping[str, Any], *, compact: bool = False) -> str:
        task = content["task"]
        work = content["work_state"]
        lines = [
            "# Task Handoff",
            "",
            "## Objective",
            str(task["objective"]),
            "",
            "## Expected output",
            str(task["expected_output"]),
        ]
        sections = [
            ("Completed", work.get("completed", [])),
            ("Remaining", work.get("remaining", [])),
            ("Critical constraints", content.get("constraints", [])),
            ("Approved decisions", content.get("decisions", [])),
            ("Blockers", work.get("blockers", [])),
        ]
        if not compact:
            sections.extend(
                [
                    (
                        "Relevant Recall Skills",
                        [
                            f"`{item['id']}@{item['version']}`"
                            for item in content.get("skills", [])
                        ],
                    ),
                    ("Important files", work.get("important_files", [])),
                    ("Known failures", work.get("known_failures", [])),
                    ("Open questions", work.get("open_questions", [])),
                ]
            )
        for title, values in sections:
            if not values:
                continue
            lines.extend(["", f"## {title}"])
            for value in values:
                if isinstance(value, Mapping):
                    value = value.get("value", value.get("statement", value))
                lines.append(f"- {value}")
        lines.extend(
            [
                "",
                "## Completion report required",
                "- Files changed",
                "- Work completed",
                "- Tests",
                "- Blockers and remaining work",
                "- New proposed decisions and questions",
            ]
        )
        return "\n".join(lines).strip() + "\n"

    def _content(
        self,
        request: HandoffRequest,
        task: Mapping[str, Any],
        session: Mapping[str, Any],
        pack: Mapping[str, Any],
    ) -> dict[str, Any]:
        checkpoint = session.get("checkpoint") or {}
        try:
            delta = self.session_service.get_delta(
                request.scope.session_id,
                known_version=request.known_checkpoint_version,
                agent_id=request.source_agent_id,
            )
        except ContinuityError as exc:
            if exc.code not in {"invalid_version", "version_too_old"}:
                raise
            delta = {
                "from_version": request.known_checkpoint_version,
                "to_version": int(session["version"]),
                "events": [],
                "checkpoint": checkpoint,
                "warning": {"code": exc.code, "message": str(exc)},
            }
        skills = [
            {
                "id": item["id"],
                "version": int(item["version"]),
                "status": item["status"],
                "delivery": item["delivery"],
                "token_estimate": int(item["token_estimate"]),
                "source_fingerprint": item["source_fingerprint"],
            }
            for item in pack.get("skills", [])
            if item.get("status") == "approved"
        ]
        return {
            "handoff_id": "",
            "version": 1,
            "schema_version": HANDOFF_SCHEMA_VERSION,
            "scope": request.scope.as_dict(),
            "agents": {
                "source_agent_id": request.source_agent_id,
                "target_agent_id": request.target_agent_id,
                "requested_by": request.requested_by,
                "target_capabilities": list(request.capabilities),
            },
            "task": {
                "title": task["title"],
                "objective": task["objective"],
                "expected_output": request.expected_output,
                "status": task["status"],
                "source_updated_at": task["updated_at"],
            },
            "continuity": {
                "source_checkpoint_version": request.known_checkpoint_version,
                "current_checkpoint_version": int(session["version"]),
                "session_delta": delta,
            },
            "work_state": {
                "completed": list(checkpoint.get("completed", [])),
                "remaining": list(checkpoint.get("remaining", [])),
                "blockers": list(checkpoint.get("blockers", [])),
                "open_questions": list(checkpoint.get("open_questions", [])),
                "important_files": list(checkpoint.get("important_files", [])),
                "known_failures": list(checkpoint.get("known_failures", [])),
            },
            "constraints": list(checkpoint.get("constraints", [])),
            "decisions": list(checkpoint.get("approved_decisions", [])),
            "proposed_decisions": list(checkpoint.get("proposed_decisions", [])),
            "skills": skills,
            "evidence": list(pack.get("references", [])),
            "warnings": list(pack.get("warnings", [])),
            "completion_contract": {
                "required_sections": [
                    "files_changed",
                    "work_completed",
                    "tests",
                    "blockers",
                    "remaining_work",
                    "new_decisions",
                    "new_questions",
                ]
            },
        }

    def create(self, request: HandoffRequest) -> dict[str, Any]:
        started = time.perf_counter()
        request = request.validated(self.settings)
        task, session = self._resolve_scope(request.scope)
        if not self._participant(session, request.source_agent_id):
            raise ContinuityError(
                "source agent must be a session participant", code="not_found"
            )
        if request.requested_by not in {request.source_agent_id, task["created_by"]}:
            raise ContinuityError(
                "requesting agent is not authorized", code="not_found"
            )
        with self._connect() as conn:
            if (
                conn.execute(
                    "SELECT 1 FROM agents WHERE id = ?", (request.target_agent_id,)
                ).fetchone()
                is None
            ):
                raise ContinuityError("target agent was not found", code="not_found")
            if request.idempotency_key:
                prior = conn.execute(
                    "SELECT * FROM handoffs WHERE user_id = ? AND idempotency_key = ?",
                    (request.scope.user_id, request.idempotency_key),
                ).fetchone()
                if prior:
                    if (
                        prior["target_agent_id"] != request.target_agent_id
                        or prior["task_id"] != request.scope.task_id
                    ):
                        raise ContinuityError(
                            "idempotency key was reused with different handoff input",
                            code="idempotency_conflict",
                        )
                    result = self.get(
                        prior["id"], scope=request.scope, agent_id=request.requested_by
                    )
                    result["idempotent_replay"] = True
                    return result
        try:
            pack = self.context_service.build(
                ContextPackRequest(
                    user_id=request.scope.user_id,
                    workspace_id=request.scope.workspace_id,
                    project_id=request.scope.project_id,
                    repository_id=request.scope.repository_id,
                    task_id=request.scope.task_id,
                    session_id=request.scope.session_id,
                    agent_id=request.target_agent_id,
                    objective=str(task["objective"]),
                    max_tokens=request.max_tokens,
                    client_capabilities=request.capabilities,
                    idempotency_key=f"handoff:{request.idempotency_key}"
                    if request.idempotency_key
                    else None,
                )
            )
            content = self._content(request, task, session, pack)
            handoff_id = f"handoff:{request.scope.task_id}:{request.target_agent_id}"
            with self._connect(immediate=True) as conn:
                existing = conn.execute(
                    "SELECT * FROM handoffs WHERE id = ?", (handoff_id,)
                ).fetchone()
                version = int(existing["current_version"]) + 1 if existing else 1
                if existing and existing["status"] not in TERMINAL_STATUSES:
                    if not self._state_stale(conn, existing, task, session):
                        raise ContinuityError(
                            "an active handoff already exists for this task and target",
                            code="conflict",
                        )
                content["handoff_id"] = handoff_id
                content["version"] = version
                now = time.time()
                expires_at = now + self.settings.HANDOFF_TTL_SECONDS
                markdown = self._render_markdown(content)
                compact = self._render_markdown(content, compact=True)
                content_hash = _digest(content)
                source_fingerprint = _digest(
                    {
                        "task_updated_at": task["updated_at"],
                        "checkpoint_version": session["version"],
                        "context_pack_fingerprint": pack["freshness"][
                            "source_fingerprint"
                        ],
                        "skill_versions": [
                            (item["id"], item["version"], item["source_fingerprint"])
                            for item in content["skills"]
                        ],
                        "policy_version": self.policy_version,
                        "schema_version": HANDOFF_SCHEMA_VERSION,
                    }
                )
                if existing:
                    self._event(
                        conn,
                        existing,
                        event_type="handoff.superseded",
                        agent_id=request.source_agent_id,
                        payload={"superseded_by_version": version},
                        idempotency_key=(
                            f"{request.idempotency_key}:superseded"
                            if request.idempotency_key
                            else None
                        ),
                    )
                    conn.execute(
                        """
                        UPDATE handoffs
                        SET source_agent_id = ?, requested_by = ?, status = ?,
                            current_version = ?, source_checkpoint_version = ?,
                            idempotency_key = ?, updated_at = ?, claimed_at = NULL,
                            completed_at = NULL, expires_at = ?
                        WHERE id = ?
                        """,
                        (
                            request.source_agent_id,
                            request.requested_by,
                            request.status,
                            version,
                            int(session["version"]),
                            request.idempotency_key,
                            now,
                            expires_at,
                            handoff_id,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO handoffs (
                            id, user_id, workspace_id, project_id, repository_id,
                            task_id, session_id, source_agent_id, target_agent_id,
                            requested_by, status, current_version,
                            source_checkpoint_version, idempotency_key, created_at,
                            updated_at, expires_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            handoff_id,
                            *self._scope_values(request.scope),
                            request.source_agent_id,
                            request.target_agent_id,
                            request.requested_by,
                            request.status,
                            version,
                            int(session["version"]),
                            request.idempotency_key,
                            now,
                            now,
                            expires_at,
                        ),
                    )
                conn.execute(
                    """
                    INSERT INTO handoff_versions (
                        handoff_id, version, schema_version, content_json,
                        content_markdown, compact_markdown, content_hash,
                        source_fingerprint, context_pack_id, token_estimate,
                        policy_version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        handoff_id,
                        version,
                        HANDOFF_SCHEMA_VERSION,
                        _stable_json(content),
                        markdown,
                        compact,
                        content_hash,
                        source_fingerprint,
                        pack["context_pack_id"],
                        _estimate(markdown),
                        self.policy_version,
                        now,
                    ),
                )
                handoff = conn.execute(
                    "SELECT * FROM handoffs WHERE id = ?", (handoff_id,)
                ).fetchone()
                assert handoff is not None
                self._event(
                    conn,
                    handoff,
                    event_type="handoff.created",
                    agent_id=request.source_agent_id,
                    payload={"target_agent_id": request.target_agent_id},
                    idempotency_key=f"{request.idempotency_key}:created"
                    if request.idempotency_key
                    else None,
                )
                if request.status == "ready":
                    self._event(
                        conn,
                        handoff,
                        event_type="handoff.ready",
                        agent_id=request.source_agent_id,
                        payload={},
                        idempotency_key=f"{request.idempotency_key}:ready"
                        if request.idempotency_key
                        else None,
                    )
                for reference in pack.get("references", []):
                    reference_id = (
                        f"handoff-ref-{len(content['evidence'])}-{uuid.uuid4().hex[:8]}"
                    )
                    conn.execute(
                        """
                        INSERT INTO handoff_references (
                            id, handoff_id, handoff_version, reference_type,
                            source_id, source_version, title, summary,
                            content_json, token_estimate, created_at
                        ) VALUES (?, ?, ?, 'context', ?, NULL, ?, ?, ?, ?, ?)
                        """,
                        (
                            reference_id,
                            handoff_id,
                            version,
                            reference["reference_id"],
                            reference.get("title", reference["reference_id"]),
                            reference.get("summary", ""),
                            _stable_json(reference),
                            int(reference.get("estimated_tokens", 0)),
                            now,
                        ),
                    )
                for index, skill in enumerate(content["skills"], start=1):
                    conn.execute(
                        """
                        INSERT INTO handoff_references (
                            id, handoff_id, handoff_version, reference_type,
                            source_id, source_version, title, summary,
                            content_json, token_estimate, created_at
                        ) VALUES (?, ?, ?, 'skill', ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"handoff-skill-{version}-{index}",
                            handoff_id,
                            version,
                            skill["id"],
                            str(skill["version"]),
                            skill["id"],
                            "Approved Recall Skill selected for this handoff.",
                            _stable_json(skill),
                            int(skill["token_estimate"]),
                            now,
                        ),
                    )
                row = conn.execute(
                    "SELECT * FROM handoff_versions WHERE handoff_id = ? AND version = ?",
                    (handoff_id, version),
                ).fetchone()
                assert row is not None
                result = self._version_dict(conn, row)
            try:
                self.session_service.append_event(
                    request.scope.session_id,
                    agent_id=request.source_agent_id,
                    event_type="handoff.created",
                    payload={
                        "handoff_id": handoff_id,
                        "version": version,
                        "target_agent_id": request.target_agent_id,
                    },
                    idempotency_key=f"handoff:{handoff_id}:{version}:created",
                )
            except Exception as exc:
                with self._metrics_lock:
                    self._metrics.last_error = (
                        f"{type(exc).__name__}: session event failed"
                    )
            with self._metrics_lock:
                self._metrics.created_total += 1
                self._metrics.latency_seconds_total += time.perf_counter() - started
            result["idempotent_replay"] = False
            return result
        except Exception as exc:
            with self._metrics_lock:
                self._metrics.build_failures_total += 1
                self._metrics.last_error = f"{type(exc).__name__}: handoff build failed"
            raise

    def get(
        self,
        handoff_id: str,
        *,
        scope: HandoffScope,
        version: int | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        self._resolve_scope(scope)
        with self._connect() as conn:
            handoff = self._authorized_row(
                conn, handoff_id, scope=scope, agent_id=agent_id
            )
            exact = int(handoff["current_version"]) if version is None else version
            row = conn.execute(
                "SELECT * FROM handoff_versions WHERE handoff_id = ? AND version = ?",
                (handoff_id, exact),
            ).fetchone()
            if row is None:
                raise ContinuityError("handoff version was not found", code="not_found")
            return self._version_dict(conn, row)

    def list_handoffs(
        self,
        *,
        scope: HandoffScope,
        agent_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        self._resolve_scope(scope)
        if status and status not in HANDOFF_STATUSES:
            raise ContinuityError("unsupported handoff status")
        query = f"SELECT * FROM handoffs WHERE {self._scope_predicate()}"
        values: list[object] = list(self._scope_values(scope))
        if agent_id:
            query += (
                " AND (source_agent_id = ? OR target_agent_id = ? OR requested_by = ?)"
            )
            values.extend([agent_id, agent_id, agent_id])
        if status:
            query += " AND status = ?"
            values.append(status)
        query += " ORDER BY created_at, id"
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(query, values).fetchall()]

    def versions(
        self, handoff_id: str, *, scope: HandoffScope, agent_id: str | None = None
    ) -> list[dict[str, Any]]:
        self._resolve_scope(scope)
        with self._connect() as conn:
            self._authorized_row(conn, handoff_id, scope=scope, agent_id=agent_id)
            rows = conn.execute(
                "SELECT * FROM handoff_versions WHERE handoff_id = ? ORDER BY version",
                (handoff_id,),
            ).fetchall()
            return [self._version_dict(conn, row) for row in rows]

    def _transition(
        self,
        handoff_id: str,
        *,
        scope: HandoffScope,
        agent_id: str,
        target_status: str,
        event_type: str,
        payload: Mapping[str, object] | None = None,
        idempotency_key: str | None = None,
        target_only: bool = False,
    ) -> dict[str, Any]:
        task, session = self._resolve_scope(scope)
        with self._connect(immediate=True) as conn:
            handoff = self._authorized_row(
                conn, handoff_id, scope=scope, agent_id=agent_id
            )
            if target_only and agent_id != handoff["target_agent_id"]:
                raise ContinuityError(
                    "only the assigned target agent may perform this action",
                    code="not_found",
                )
            now = time.time()
            if (
                target_status != "expired"
                and handoff["status"] not in TERMINAL_STATUSES
                and handoff["expires_at"] is not None
                and float(handoff["expires_at"]) <= now
            ):
                conn.execute(
                    "UPDATE handoffs SET status = 'expired', updated_at = ? WHERE id = ?",
                    (now, handoff_id),
                )
                expired = conn.execute(
                    "SELECT * FROM handoffs WHERE id = ?", (handoff_id,)
                ).fetchone()
                assert expired is not None
                event = self._event(
                    conn,
                    expired,
                    event_type="handoff.expired",
                    agent_id=agent_id,
                    payload={"reason": "ttl_elapsed"},
                    idempotency_key=f"auto-expire:{handoff_id}:{handoff['current_version']}",
                )
                with self._metrics_lock:
                    self._metrics.expired_total += 1
                return {
                    "handoff_id": handoff_id,
                    "status": "expired",
                    "event": event,
                    "idempotent_replay": False,
                }
            if target_status in {
                "claimed",
                "in_progress",
                "blocked",
            } and self._state_stale(conn, handoff, task, session):
                conn.execute(
                    "UPDATE handoffs SET status = 'superseded', updated_at = ? WHERE id = ?",
                    (now, handoff_id),
                )
                stale = conn.execute(
                    "SELECT * FROM handoffs WHERE id = ?", (handoff_id,)
                ).fetchone()
                assert stale is not None
                event = self._event(
                    conn,
                    stale,
                    event_type="handoff.superseded",
                    agent_id=agent_id,
                    payload={"reason": "source_state_changed"},
                    idempotency_key=f"auto-supersede:{handoff_id}:{handoff['current_version']}",
                )
                return {
                    "handoff_id": handoff_id,
                    "status": "superseded",
                    "stale_reason": "source_state_changed",
                    "event": event,
                    "idempotent_replay": False,
                }
            if handoff["status"] == target_status and idempotency_key:
                event = self._event(
                    conn,
                    handoff,
                    event_type=event_type,
                    agent_id=agent_id,
                    payload=payload,
                    idempotency_key=idempotency_key,
                )
                return {
                    "handoff_id": handoff_id,
                    "status": target_status,
                    "event": event,
                    "idempotent_replay": True,
                }
            if target_status not in TRANSITIONS.get(str(handoff["status"]), set()):
                raise ContinuityError(
                    f"cannot transition handoff from {handoff['status']} to {target_status}",
                    code="conflict",
                )
            conn.execute(
                """
                UPDATE handoffs SET status = ?, updated_at = ?,
                    claimed_at = CASE WHEN ? = 'claimed' THEN COALESCE(claimed_at, ?) ELSE claimed_at END,
                    completed_at = CASE WHEN ? = 'completed' THEN ? ELSE completed_at END
                WHERE id = ?
                """,
                (
                    target_status,
                    now,
                    target_status,
                    now,
                    target_status,
                    now,
                    handoff_id,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM handoffs WHERE id = ?", (handoff_id,)
            ).fetchone()
            assert updated is not None
            event = self._event(
                conn,
                updated,
                event_type=event_type,
                agent_id=agent_id,
                payload=payload,
                idempotency_key=idempotency_key,
            )
            return {
                "handoff_id": handoff_id,
                "status": target_status,
                "event": event,
                "idempotent_replay": False,
            }

    def claim(
        self,
        handoff_id: str,
        *,
        scope: HandoffScope,
        agent_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        result = self._transition(
            handoff_id,
            scope=scope,
            agent_id=agent_id,
            target_status="claimed",
            event_type="handoff.claimed",
            idempotency_key=idempotency_key,
            target_only=True,
        )
        if result["status"] != "claimed":
            return result
        self.session_service.join_session(
            scope.session_id,
            agent_id=agent_id,
            role="implementer",
            idempotency_key=f"handoff:{handoff_id}:joined",
        )
        self.session_service.append_event(
            scope.session_id,
            agent_id=agent_id,
            event_type="handoff.claimed",
            payload={"handoff_id": handoff_id},
            idempotency_key=f"handoff:{handoff_id}:claimed",
        )
        with self._metrics_lock:
            self._metrics.claimed_total += int(not result["idempotent_replay"])
        return result

    def progress(
        self,
        handoff_id: str,
        *,
        scope: HandoffScope,
        agent_id: str,
        progress: Mapping[str, object],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._transition(
            handoff_id,
            scope=scope,
            agent_id=agent_id,
            target_status="in_progress",
            event_type="handoff.progress",
            payload=progress,
            idempotency_key=idempotency_key,
            target_only=True,
        )

    def block(
        self,
        handoff_id: str,
        *,
        scope: HandoffScope,
        agent_id: str,
        blocker: Mapping[str, object],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        result = self._transition(
            handoff_id,
            scope=scope,
            agent_id=agent_id,
            target_status="blocked",
            event_type="handoff.blocked",
            payload=blocker,
            idempotency_key=idempotency_key,
            target_only=True,
        )
        if result["status"] != "blocked":
            return result
        self.session_service.append_event(
            scope.session_id,
            agent_id=agent_id,
            event_type="blocker.reported",
            payload={"kind": "handoff.blocked", "handoff_id": handoff_id, **blocker},
            idempotency_key=f"handoff:{handoff_id}:blocked:{idempotency_key or 'once'}",
        )
        with self._metrics_lock:
            self._metrics.blocked_total += int(not result["idempotent_replay"])
        return result

    def cancel(
        self,
        handoff_id: str,
        *,
        scope: HandoffScope,
        agent_id: str,
        reason: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            row = self._authorized_row(conn, handoff_id, scope=scope, agent_id=agent_id)
            if agent_id not in {row["source_agent_id"], row["requested_by"]}:
                raise ContinuityError(
                    "only the source or requester may cancel", code="not_found"
                )
        return self._transition(
            handoff_id,
            scope=scope,
            agent_id=agent_id,
            target_status="cancelled",
            event_type="handoff.cancelled",
            payload={"reason": reason},
            idempotency_key=idempotency_key,
        )

    def reject(
        self,
        handoff_id: str,
        *,
        scope: HandoffScope,
        agent_id: str,
        reason: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        result = self._transition(
            handoff_id,
            scope=scope,
            agent_id=agent_id,
            target_status="rejected",
            event_type="handoff.rejected",
            payload={"reason": reason},
            idempotency_key=idempotency_key,
            target_only=True,
        )
        with self._metrics_lock:
            self._metrics.rejected_total += int(not result["idempotent_replay"])
        return result

    def expire(
        self,
        handoff_id: str,
        *,
        scope: HandoffScope,
        agent_id: str,
        reason: str = "expired",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        result = self._transition(
            handoff_id,
            scope=scope,
            agent_id=agent_id,
            target_status="expired",
            event_type="handoff.expired",
            payload={"reason": reason},
            idempotency_key=idempotency_key,
        )
        with self._metrics_lock:
            self._metrics.expired_total += int(not result["idempotent_replay"])
        return result

    def complete(
        self,
        handoff_id: str,
        *,
        scope: HandoffScope,
        agent_id: str,
        completion: HandoffCompletion,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            task, source_session = self._resolve_scope(scope)
            payload = {
                "files_changed": _list("files_changed", completion.files_changed),
                "work_completed": _list("work_completed", completion.work_completed),
                "tests": dict(completion.tests or {}),
                "blockers": _list("blockers", completion.blockers),
                "remaining_work": _list("remaining_work", completion.remaining_work),
                "new_decisions": _list("new_decisions", completion.new_decisions),
                "new_questions": _list("new_questions", completion.new_questions),
                "used_skill_versions": list(completion.used_skill_versions),
                "used_memory_ids": list(completion.used_memory_ids),
                "expanded_references": list(completion.expanded_references),
            }
            with self._connect() as conn:
                handoff = self._authorized_row(
                    conn, handoff_id, scope=scope, agent_id=agent_id
                )
                if agent_id != handoff["target_agent_id"]:
                    raise ContinuityError(
                        "only the assigned target agent may complete", code="not_found"
                    )
                version = int(handoff["current_version"])
                prior = conn.execute(
                    "SELECT * FROM handoff_completions WHERE handoff_id = ? AND handoff_version = ?",
                    (handoff_id, version),
                ).fetchone()
                if prior:
                    prior_payload = self._completion_dict(prior)
                    if any(prior_payload[key] != payload[key] for key in payload):
                        raise ContinuityError(
                            "completion retry did not match the original report",
                            code="idempotency_conflict",
                        )
                    return {
                        **prior_payload,
                        "idempotent_replay": True,
                        "delta": self.get_delta(
                            handoff_id,
                            scope=scope,
                            known_version=version,
                            agent_id=agent_id,
                        ),
                    }
                if handoff["status"] not in {"claimed", "in_progress", "blocked"}:
                    raise ContinuityError(
                        "handoff cannot be completed from its current status",
                        code="conflict",
                    )
                expired_due = (
                    handoff["expires_at"] is not None
                    and float(handoff["expires_at"]) <= time.time()
                )
                stale_due = self._state_stale(conn, handoff, task, source_session)
            if expired_due or stale_due:
                terminal = self._transition(
                    handoff_id,
                    scope=scope,
                    agent_id=agent_id,
                    target_status="in_progress",
                    event_type="handoff.progress",
                    payload={"reason": "completion_source_check"},
                    target_only=True,
                )
                raise ContinuityError(
                    f"handoff is {terminal['status']}; create a fresh version",
                    code="conflict",
                )
            session = self.session_service.get_session(scope.session_id)
            checkpoint = session.get("checkpoint") or {}
            event = self.session_service.append_event(
                scope.session_id,
                agent_id=agent_id,
                event_type="handoff.completed",
                payload={
                    "handoff_id": handoff_id,
                    "handoff_version": version,
                    "work_completed": payload["work_completed"],
                    "files_changed": payload["files_changed"],
                    "tests": payload["tests"],
                },
                idempotency_key=f"handoff:{handoff_id}:{version}:completed",
            )
            proposed = list(checkpoint.get("proposed_decisions", []))
            proposed.extend(
                {
                    "statement": item,
                    "status": "proposed",
                    "source_agent_id": agent_id,
                    "handoff_id": handoff_id,
                }
                for item in payload["new_decisions"]
            )
            new_checkpoint = self.session_service.create_checkpoint(
                scope.session_id,
                agent_id=agent_id,
                summary=f"Handoff {handoff_id} completed by {agent_id}",
                completed=[
                    *checkpoint.get("completed", []),
                    *payload["work_completed"],
                ],
                remaining=list(payload["remaining_work"]),
                constraints=checkpoint.get("constraints", []),
                approved_decisions=checkpoint.get("approved_decisions", []),
                proposed_decisions=proposed,
                open_questions=[
                    *checkpoint.get("open_questions", []),
                    *payload["new_questions"],
                ],
                blockers=list(payload["blockers"]),
                important_files=[
                    *checkpoint.get("important_files", []),
                    *payload["files_changed"],
                ],
                known_failures=checkpoint.get("known_failures", []),
                idempotency_key=f"handoff:{handoff_id}:{version}:checkpoint",
            )
            with self._connect(immediate=True) as conn:
                handoff = self._authorized_row(
                    conn, handoff_id, scope=scope, agent_id=agent_id
                )
                now = time.time()
                conn.execute(
                    """
                    INSERT INTO handoff_completions (
                        handoff_id, handoff_version, agent_id, status,
                        files_changed_json, work_completed_json, tests_json,
                        blockers_json, remaining_work_json, new_decisions_json,
                        new_questions_json, used_skill_versions_json,
                        used_memory_ids_json, expanded_references_json,
                        idempotency_key, created_at
                    ) VALUES (?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        handoff_id,
                        version,
                        agent_id,
                        _stable_json(payload["files_changed"]),
                        _stable_json(payload["work_completed"]),
                        _stable_json(payload["tests"]),
                        _stable_json(payload["blockers"]),
                        _stable_json(payload["remaining_work"]),
                        _stable_json(payload["new_decisions"]),
                        _stable_json(payload["new_questions"]),
                        _stable_json(payload["used_skill_versions"]),
                        _stable_json(payload["used_memory_ids"]),
                        _stable_json(payload["expanded_references"]),
                        idempotency_key,
                        now,
                    ),
                )
                conn.execute(
                    "UPDATE handoffs SET status = 'completed', completed_at = ?, updated_at = ? WHERE id = ?",
                    (now, now, handoff_id),
                )
                updated = conn.execute(
                    "SELECT * FROM handoffs WHERE id = ?", (handoff_id,)
                ).fetchone()
                assert updated is not None
                self._event(
                    conn,
                    updated,
                    event_type="handoff.completed",
                    agent_id=agent_id,
                    payload={
                        "session_event_id": event["id"],
                        "checkpoint_version": new_checkpoint["version"],
                    },
                    idempotency_key=f"{idempotency_key}:event"
                    if idempotency_key
                    else None,
                )
            feedback_warning = None
            try:
                self._record_reported_usage(
                    handoff_id, version, scope, agent_id, payload
                )
            except Exception as exc:
                feedback_warning = {
                    "code": "feedback_unavailable",
                    "message": "Canonical completion succeeded but secondary usage feedback failed.",
                }
                with self._metrics_lock:
                    self._metrics.last_error = (
                        f"{type(exc).__name__}: handoff feedback failed"
                    )
            result = {
                "handoff_id": handoff_id,
                "handoff_version": version,
                "agent_id": agent_id,
                "status": "completed",
                **payload,
                "session_event_id": event["id"],
                "checkpoint_version": new_checkpoint["version"],
                "delta": self.get_delta(
                    handoff_id, scope=scope, known_version=version, agent_id=agent_id
                ),
                "idempotent_replay": False,
                **({"warning": feedback_warning} if feedback_warning else {}),
            }
            with self._metrics_lock:
                self._metrics.completed_total += 1
                self._metrics.latency_seconds_total += time.perf_counter() - started
            return result
        except Exception as exc:
            with self._metrics_lock:
                self._metrics.completion_failures_total += 1
                self._metrics.last_error = (
                    f"{type(exc).__name__}: handoff completion failed"
                )
            raise

    @staticmethod
    def _completion_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for key in (
            "files_changed",
            "work_completed",
            "tests",
            "blockers",
            "remaining_work",
            "new_decisions",
            "new_questions",
            "used_skill_versions",
            "used_memory_ids",
            "expanded_references",
        ):
            result[key] = json.loads(result.pop(f"{key}_json"))
        return result

    def _record_reported_usage(
        self,
        handoff_id: str,
        version: int,
        scope: HandoffScope,
        agent_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        pack = self.get(handoff_id, scope=scope, version=version, agent_id=agent_id)
        selected = {
            f"{item['id']}@{item['version']}": item for item in pack.get("skills", [])
        }
        skill_scope = SkillScope(
            scope.user_id, scope.workspace_id, scope.project_id, scope.repository_id
        )
        for exact in payload["used_skill_versions"]:
            item = selected.get(exact)
            if not item:
                raise ContinuityError(
                    "completion reported an unselected skill version",
                    code="scope_mismatch",
                )
            self.skill_service.record_usage(
                item["id"],
                int(item["version"]),
                scope=skill_scope,
                usage_type="used",
                agent_id=agent_id,
                context_pack_id=pack["context_pack_id"],
                task_id=scope.task_id,
                session_id=scope.session_id,
                metadata={"handoff_id": handoff_id},
                idempotency_key=f"handoff:{handoff_id}:{version}:used:{exact}",
            )
            with self._metrics_lock:
                self._metrics.skill_usage_total += 1
        if payload["used_memory_ids"]:
            self.context_service.record_feedback(
                context_pack_id=pack["context_pack_id"],
                scope=scope.as_dict(),
                agent_id=agent_id,
                used_memory_ids=payload["used_memory_ids"],
                idempotency_key=f"handoff:{handoff_id}:{version}:memory-usage",
            )

    def get_delta(
        self,
        handoff_id: str,
        *,
        scope: HandoffScope,
        known_version: int,
        agent_id: str | None = None,
        known_checkpoint_version: int | None = None,
    ) -> dict[str, Any]:
        if known_version < 0:
            raise ContinuityError("known_version must be zero or greater")
        self._resolve_scope(scope)
        with self._connect() as conn:
            handoff = self._authorized_row(
                conn, handoff_id, scope=scope, agent_id=agent_id
            )
            current = int(handoff["current_version"])
            if known_version > current:
                raise ContinuityError(
                    "known_version is newer than the handoff", code="invalid_version"
                )
            events = []
            for row in conn.execute(
                "SELECT * FROM handoff_events WHERE handoff_id = ? AND version >= ? ORDER BY created_at, id",
                (handoff_id, max(1, known_version)),
            ).fetchall():
                item = dict(row)
                item["payload"] = json.loads(item.pop("payload_json"))
                events.append(item)
            completion_row = conn.execute(
                "SELECT * FROM handoff_completions WHERE handoff_id = ? ORDER BY handoff_version DESC LIMIT 1",
                (handoff_id,),
            ).fetchone()
        session_delta = None
        if known_checkpoint_version is not None:
            session_delta = self.session_service.get_delta(
                scope.session_id,
                known_version=known_checkpoint_version,
                agent_id=agent_id,
            )
        return {
            "handoff_id": handoff_id,
            "from_version": known_version,
            "to_version": current,
            "status": "current" if not events and completion_row is None else "changed",
            "events": events,
            "completion": self._completion_dict(completion_row)
            if completion_row
            else None,
            "session_delta": session_delta,
        }

    def expand_reference(
        self, handoff_id: str, reference_id: str, *, scope: HandoffScope, agent_id: str
    ) -> dict[str, Any]:
        pack = self.get(handoff_id, scope=scope, agent_id=agent_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM handoff_references WHERE id = ? AND handoff_id = ?",
                (reference_id, handoff_id),
            ).fetchone()
        if row is None:
            raise ContinuityError("handoff reference was not found", code="not_found")
        if row["reference_type"] == "context":
            result = self.context_service.expand_reference(
                pack["context_pack_id"],
                row["source_id"],
                scope.as_dict(),
                agent_id=agent_id,
            )
        elif row["reference_type"] == "skill":
            result = self.skill_service.expand_evidence(
                row["source_id"],
                int(row["source_version"]),
                scope=SkillScope(
                    scope.user_id,
                    scope.workspace_id,
                    scope.project_id,
                    scope.repository_id,
                ),
                agent_id=agent_id,
                context_pack_id=pack["context_pack_id"],
                idempotency_key=f"handoff:{handoff_id}:expand:{reference_id}",
            )
        else:
            result = json.loads(row["content_json"])
        with self._metrics_lock:
            self._metrics.reference_expansions_total += 1
        return {
            "handoff_id": handoff_id,
            "reference_id": reference_id,
            "content": result,
        }

    def explain(
        self,
        handoff_id: str,
        *,
        scope: HandoffScope,
        version: int | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        pack = self.get(handoff_id, scope=scope, version=version, agent_id=agent_id)
        return {
            "handoff_id": handoff_id,
            "version": pack["version"],
            "scope_filters": scope.as_dict(),
            "target_agent_id": pack["agents"]["target_agent_id"],
            "policy_version": pack["policy_version"],
            "content_hash": pack["content_hash"],
            "source_fingerprint": pack["source_fingerprint"],
            "selection_order": [
                "objective",
                "expected_output",
                "critical_constraints",
                "approved_decisions",
                "blockers",
                "work_state",
                "session_delta",
                "approved_skills",
                "evidence",
            ],
            "skill_versions": [
                f"{item['id']}@{item['version']}" for item in pack.get("skills", [])
            ],
            "warnings": pack.get("warnings", []),
            "full_transcript_required": False,
        }

    def stats(self) -> dict[str, object]:
        with self._connect() as conn:
            counts = {
                row["status"]: int(row["count"])
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS count FROM handoffs GROUP BY status"
                ).fetchall()
            }
            oldest = conn.execute(
                "SELECT MIN(created_at) AS created_at FROM handoffs WHERE status IN ('draft', 'ready')"
            ).fetchone()
        with self._metrics_lock:
            metrics = asdict(self._metrics)
        return {
            **metrics,
            "counts": counts,
            "in_progress": counts.get("in_progress", 0),
            "blocked": counts.get("blocked", 0),
            "expired": counts.get("expired", 0),
            "oldest_pending_age_seconds": max(0.0, time.time() - oldest["created_at"])
            if oldest and oldest["created_at"]
            else 0.0,
            "policy_version": self.policy_version,
        }

    def health(self) -> dict[str, object]:
        with self._connect() as conn:
            migration = conn.execute(
                "SELECT MAX(version) AS version FROM schema_migrations"
            ).fetchone()
        return {
            "available": True,
            "migration_available": int(migration["version"] or 0)
            >= CURRENT_SCHEMA_VERSION,
            "policy_version": self.policy_version,
            **self.stats(),
        }
