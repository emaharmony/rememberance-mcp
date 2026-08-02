"""Canonical task and cross-agent session continuity services."""

from __future__ import annotations

import json
import hashlib
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from recall_mcp.store.migrations import run_migrations


TASK_STATUSES = {
    "planned",
    "active",
    "blocked",
    "review",
    "completed",
    "cancelled",
}
PARTICIPANT_ROLES = {
    "owner",
    "orchestrator",
    "implementer",
    "reviewer",
    "observer",
}
SESSION_EVENT_TYPES = {
    "session.started",
    "session.closed",
    "agent.joined",
    "agent.left",
    "task.updated",
    "checkpoint.created",
    "decision.proposed",
    "decision.approved",
    "blocker.reported",
    "work.completed",
    "validation.requested",
}


class ContinuityError(ValueError):
    """A safe application error returned by every continuity transport."""

    def __init__(self, message: str, *, code: str = "invalid_request"):
        super().__init__(message)
        self.code = code


def _identifier(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _decoded(value: str | None, default: object) -> Any:
    if not value:
        return default
    return json.loads(value)


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class ContinuityStore:
    """Small SQLite boundary shared by the task and session services."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        run_migrations(self.db_path)

    @contextmanager
    def connect(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
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


class TaskService:
    """Own task identity and enforce the user/workspace/project/repository scope."""

    def __init__(self, store: ContinuityStore):
        self.store = store

    @staticmethod
    def _require_text(name: str, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ContinuityError(f"{name} must be a non-empty string")
        return value.strip()

    def _ensure_agent(
        self,
        conn: sqlite3.Connection,
        agent_id: str,
        *,
        name: str | None = None,
        system_type: str = "other",
    ) -> None:
        now = time.time()
        existing = conn.execute(
            "SELECT id FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE agents SET last_seen_at = ? WHERE id = ?",
                (now, agent_id),
            )
            return
        conn.execute(
            """
            INSERT INTO agents (
                id, name, system_type, trust_level, write_policy,
                created_at, last_seen_at
            ) VALUES (?, ?, ?, 'standard', 'standard', ?, ?)
            """,
            (agent_id, name or agent_id, system_type or "other", now, now),
        )

    @staticmethod
    def _scope_mismatch(kind: str, identifier: str) -> ContinuityError:
        return ContinuityError(
            f"{kind} {identifier} belongs to a different scope",
            code="scope_mismatch",
        )

    def _ensure_scope(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: str,
        workspace_id: str,
        project_id: str,
        repository_id: str,
        workspace_name: str | None = None,
        project_name: str | None = None,
        repository_name: str | None = None,
        canonical_path: str | None = None,
        remote_url: str | None = None,
        default_branch: str | None = None,
    ) -> None:
        now = time.time()
        workspace = conn.execute(
            "SELECT user_id FROM workspaces WHERE id = ?", (workspace_id,)
        ).fetchone()
        if workspace and workspace["user_id"] != user_id:
            raise self._scope_mismatch("workspace", workspace_id)
        if not workspace:
            conn.execute(
                """
                INSERT INTO workspaces (id, user_id, name, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (workspace_id, user_id, workspace_name or workspace_id, now, now),
            )

        project = conn.execute(
            "SELECT user_id, workspace_id FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        if project and (
            project["user_id"] != user_id or project["workspace_id"] != workspace_id
        ):
            raise self._scope_mismatch("project", project_id)
        if not project:
            conn.execute(
                """
                INSERT INTO projects (
                    id, user_id, workspace_id, name, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    project_id,
                    user_id,
                    workspace_id,
                    project_name or project_id,
                    now,
                    now,
                ),
            )

        repository = conn.execute(
            """
            SELECT user_id, workspace_id, project_id
            FROM repositories WHERE id = ?
            """,
            (repository_id,),
        ).fetchone()
        if repository and (
            repository["user_id"] != user_id
            or repository["workspace_id"] != workspace_id
            or repository["project_id"] != project_id
        ):
            raise self._scope_mismatch("repository", repository_id)
        if not repository:
            conn.execute(
                """
                INSERT INTO repositories (
                    id, user_id, workspace_id, project_id, name, canonical_path,
                    remote_url, default_branch, access_mode, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'read_only', ?, ?)
                """,
                (
                    repository_id,
                    user_id,
                    workspace_id,
                    project_id,
                    repository_name or repository_id,
                    canonical_path or repository_id,
                    remote_url,
                    default_branch,
                    now,
                    now,
                ),
            )

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    def create_task(
        self,
        *,
        user_id: str,
        workspace_id: str,
        project_id: str,
        repository_id: str,
        title: str,
        objective: str,
        created_by: str,
        status: str = "active",
        idempotency_key: str | None = None,
        agent_name: str | None = None,
        agent_system_type: str = "other",
        workspace_name: str | None = None,
        project_name: str | None = None,
        repository_name: str | None = None,
        canonical_path: str | None = None,
        remote_url: str | None = None,
        default_branch: str | None = None,
    ) -> dict[str, Any]:
        values = {
            "user_id": user_id,
            "workspace_id": workspace_id,
            "project_id": project_id,
            "repository_id": repository_id,
            "title": title,
            "objective": objective,
            "created_by": created_by,
        }
        clean = {key: self._require_text(key, value) for key, value in values.items()}
        if status not in TASK_STATUSES:
            raise ContinuityError("unsupported task status")
        with self.store.connect(immediate=True) as conn:
            self._ensure_agent(
                conn,
                clean["created_by"],
                name=agent_name,
                system_type=agent_system_type,
            )
            self._ensure_scope(
                conn,
                user_id=clean["user_id"],
                workspace_id=clean["workspace_id"],
                project_id=clean["project_id"],
                repository_id=clean["repository_id"],
                workspace_name=workspace_name,
                project_name=project_name,
                repository_name=repository_name,
                canonical_path=canonical_path,
                remote_url=remote_url,
                default_branch=default_branch,
            )
            if idempotency_key:
                prior = conn.execute(
                    """
                    SELECT * FROM tasks
                    WHERE user_id = ? AND idempotency_key = ?
                    """,
                    (clean["user_id"], idempotency_key),
                ).fetchone()
                if prior:
                    for field in (
                        "workspace_id",
                        "project_id",
                        "repository_id",
                        "title",
                        "objective",
                        "created_by",
                    ):
                        if prior[field] != clean[field]:
                            raise ContinuityError(
                                "idempotency key was reused with different task input",
                                code="idempotency_conflict",
                            )
                    result = self._task_from_row(prior)
                    result["idempotent_replay"] = True
                    return result
            now = time.time()
            task_id = _identifier("task")
            conn.execute(
                """
                INSERT INTO tasks (
                    id, user_id, workspace_id, project_id, repository_id,
                    title, objective, status, created_by, idempotency_key,
                    created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    clean["user_id"],
                    clean["workspace_id"],
                    clean["project_id"],
                    clean["repository_id"],
                    clean["title"],
                    clean["objective"],
                    status,
                    clean["created_by"],
                    idempotency_key,
                    now,
                    now,
                    now if status == "completed" else None,
                ),
            )
            created = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            assert created is not None
            result = self._task_from_row(created)
            result["idempotent_replay"] = False
            return result

    def get_task(
        self,
        task_id: str,
        *,
        user_id: str | None = None,
        project_id: str | None = None,
        repository_id: str | None = None,
    ) -> dict[str, Any]:
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise ContinuityError(f"task {task_id} was not found", code="not_found")
        for field, expected in (
            ("user_id", user_id),
            ("project_id", project_id),
            ("repository_id", repository_id),
        ):
            if expected is not None and row[field] != expected:
                raise ContinuityError(
                    "task is outside the requested scope", code="not_found"
                )
        return self._task_from_row(row)

    def update_task(
        self,
        task_id: str,
        *,
        title: str | None = None,
        objective: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        if title is None and objective is None and status is None:
            raise ContinuityError("at least one task field must be updated")
        if status is not None and status not in TASK_STATUSES:
            raise ContinuityError("unsupported task status")
        updates: list[str] = []
        params: list[object] = []
        if title is not None:
            updates.append("title = ?")
            params.append(self._require_text("title", title))
        if objective is not None:
            updates.append("objective = ?")
            params.append(self._require_text("objective", objective))
        if status is not None:
            updates.append("status = ?")
            params.append(status)
            updates.append("completed_at = ?")
            params.append(time.time() if status == "completed" else None)
        updates.append("updated_at = ?")
        params.append(time.time())
        params.append(task_id)
        with self.store.connect(immediate=True) as conn:
            cursor = conn.execute(
                f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", params
            )
            if cursor.rowcount != 1:
                raise ContinuityError(f"task {task_id} was not found", code="not_found")
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            assert row is not None
            return self._task_from_row(row)

    def complete_task(self, task_id: str) -> dict[str, Any]:
        return self.update_task(task_id, status="completed")


class SessionService:
    """Own ordered session events, checkpoints, participants, and deltas."""

    def __init__(self, store: ContinuityStore, tasks: TaskService):
        self.store = store
        self.tasks = tasks

    @staticmethod
    def _session_row(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise ContinuityError(
                f"session {session_id} was not found", code="not_found"
            )
        return row

    @staticmethod
    def _participant(
        conn: sqlite3.Connection, session_id: str, agent_id: str
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT * FROM session_participants
            WHERE session_id = ? AND agent_id = ?
            """,
            (session_id, agent_id),
        ).fetchone()

    @staticmethod
    def _next_sequence(conn: sqlite3.Connection, session: sqlite3.Row) -> int:
        sequence = int(session["next_event_sequence"])
        conn.execute(
            "UPDATE sessions SET next_event_sequence = ?, updated_at = ? WHERE id = ?",
            (sequence + 1, time.time(), session["id"]),
        )
        return sequence

    @staticmethod
    def _event_dict(row: sqlite3.Row, *, replay: bool = False) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = _decoded(result.pop("payload_json"), {})
        result["idempotent_replay"] = replay
        return result

    def _insert_event(
        self,
        conn: sqlite3.Connection,
        *,
        session: sqlite3.Row,
        agent_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        if event_type not in SESSION_EVENT_TYPES:
            raise ContinuityError("unsupported session event type")
        if idempotency_key:
            existing = conn.execute(
                """
                SELECT * FROM session_events
                WHERE session_id = ? AND idempotency_key = ?
                """,
                (session["id"], idempotency_key),
            ).fetchone()
            if existing:
                if existing["event_type"] != event_type or _decoded(
                    existing["payload_json"], {}
                ) != dict(payload or {}):
                    raise ContinuityError(
                        "idempotency key was reused with different event input",
                        code="idempotency_conflict",
                    )
                return self._event_dict(existing, replay=True)
        sequence = self._next_sequence(conn, session)
        event_id = _identifier("event")
        now = time.time()
        conn.execute(
            """
            INSERT INTO session_events (
                id, session_id, task_id, agent_id, sequence, event_type,
                payload_json, idempotency_key, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                session["id"],
                session["task_id"],
                agent_id,
                sequence,
                event_type,
                _json(dict(payload or {})),
                idempotency_key,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM session_events WHERE id = ?", (event_id,)
        ).fetchone()
        assert row is not None
        return self._event_dict(row)

    def start_session(
        self,
        *,
        task_id: str,
        agent_id: str,
        role: str = "owner",
        idempotency_key: str | None = None,
        agent_name: str | None = None,
        agent_system_type: str = "other",
    ) -> dict[str, Any]:
        if role not in PARTICIPANT_ROLES:
            raise ContinuityError("unsupported participant role")
        with self.store.connect(immediate=True) as conn:
            task = conn.execute(
                "SELECT id FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise ContinuityError(f"task {task_id} was not found", code="not_found")
            self.tasks._ensure_agent(
                conn, agent_id, name=agent_name, system_type=agent_system_type
            )
            if idempotency_key:
                prior = conn.execute(
                    """
                    SELECT id FROM sessions
                    WHERE task_id = ? AND idempotency_key = ?
                    """,
                    (task_id, idempotency_key),
                ).fetchone()
                if prior:
                    result = self._get_session(conn, prior["id"])
                    result["idempotent_replay"] = True
                    return result
            now = time.time()
            session_id = _identifier("session")
            conn.execute(
                """
                INSERT INTO sessions (
                    id, task_id, initiating_agent_id, status, started_at,
                    updated_at, summary, version, next_event_sequence,
                    idempotency_key
                ) VALUES (?, ?, ?, 'active', ?, ?, '', 0, 1, ?)
                """,
                (session_id, task_id, agent_id, now, now, idempotency_key),
            )
            conn.execute(
                """
                INSERT INTO session_participants (
                    session_id, agent_id, role, joined_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, agent_id, role, now, now),
            )
            session = self._session_row(conn, session_id)
            self._insert_event(
                conn,
                session=session,
                agent_id=agent_id,
                event_type="session.started",
                payload={"role": role},
                idempotency_key=(
                    f"{idempotency_key}:started" if idempotency_key else None
                ),
            )
            result = self._get_session(conn, session_id)
            result["idempotent_replay"] = False
            return result

    def join_session(
        self,
        session_id: str,
        *,
        agent_id: str,
        role: str = "implementer",
        idempotency_key: str | None = None,
        agent_name: str | None = None,
        agent_system_type: str = "other",
    ) -> dict[str, Any]:
        if role not in PARTICIPANT_ROLES:
            raise ContinuityError("unsupported participant role")
        with self.store.connect(immediate=True) as conn:
            session = self._session_row(conn, session_id)
            self.tasks._ensure_agent(
                conn, agent_id, name=agent_name, system_type=agent_system_type
            )
            existing = self._participant(conn, session_id, agent_id)
            now = time.time()
            if existing:
                conn.execute(
                    """
                    UPDATE session_participants SET last_seen_at = ?
                    WHERE session_id = ? AND agent_id = ?
                    """,
                    (now, session_id, agent_id),
                )
                result = self._get_session(conn, session_id)
                result["idempotent_replay"] = True
                return result
            if session["status"] != "active":
                raise ContinuityError("closed sessions cannot accept new participants")
            conn.execute(
                """
                INSERT INTO session_participants (
                    session_id, agent_id, role, joined_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, agent_id, role, now, now),
            )
            self._insert_event(
                conn,
                session=session,
                agent_id=agent_id,
                event_type="agent.joined",
                payload={"role": role},
                idempotency_key=idempotency_key,
            )
            result = self._get_session(conn, session_id)
            result["idempotent_replay"] = False
            return result

    def append_event(
        self,
        session_id: str,
        *,
        agent_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        with self.store.connect(immediate=True) as conn:
            session = self._session_row(conn, session_id)
            if session["status"] != "active":
                raise ContinuityError("closed sessions are read-only")
            if self._participant(conn, session_id, agent_id) is None:
                raise ContinuityError("agent must join the session before writing")
            return self._insert_event(
                conn,
                session=session,
                agent_id=agent_id,
                event_type=event_type,
                payload=payload,
                idempotency_key=idempotency_key,
            )

    @staticmethod
    def _checkpoint_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for column in (
            "completed_json",
            "remaining_json",
            "constraints_json",
            "approved_decisions_json",
            "proposed_decisions_json",
            "open_questions_json",
            "blockers_json",
            "important_files_json",
            "known_failures_json",
            "repository_json",
        ):
            result[column.removesuffix("_json")] = _decoded(
                result.pop(column), {} if column == "repository_json" else []
            )
        result["generated"] = bool(result["generated"])
        return result

    @staticmethod
    def _as_list(name: str, value: Sequence[object] | None) -> list[object]:
        if value is None:
            return []
        if isinstance(value, (str, bytes)):
            raise ContinuityError(f"{name} must be an array")
        return list(value)

    def create_checkpoint(
        self,
        session_id: str,
        *,
        agent_id: str,
        summary: str = "",
        completed: Sequence[object] | None = None,
        remaining: Sequence[object] | None = None,
        constraints: Sequence[object] | None = None,
        approved_decisions: Sequence[object] | None = None,
        proposed_decisions: Sequence[object] | None = None,
        open_questions: Sequence[object] | None = None,
        blockers: Sequence[object] | None = None,
        important_files: Sequence[object] | None = None,
        known_failures: Sequence[object] | None = None,
        idempotency_key: str | None = None,
        generated: bool = False,
    ) -> dict[str, Any]:
        fields = {
            "completed": self._as_list("completed", completed),
            "remaining": self._as_list("remaining", remaining),
            "constraints": self._as_list("constraints", constraints),
            "approved_decisions": self._as_list(
                "approved_decisions", approved_decisions
            ),
            "proposed_decisions": self._as_list(
                "proposed_decisions", proposed_decisions
            ),
            "open_questions": self._as_list("open_questions", open_questions),
            "blockers": self._as_list("blockers", blockers),
            "important_files": self._as_list("important_files", important_files),
            "known_failures": self._as_list("known_failures", known_failures),
        }
        state_digest = hashlib.sha256(
            _json(
                {
                    "summary": summary,
                    "generated": generated,
                    **fields,
                }
            ).encode("utf-8")
        ).hexdigest()
        with self.store.connect(immediate=True) as conn:
            session = self._session_row(conn, session_id)
            if session["status"] != "active":
                raise ContinuityError("closed sessions cannot create checkpoints")
            if self._participant(conn, session_id, agent_id) is None:
                raise ContinuityError("agent must join the session before writing")
            if idempotency_key:
                event = conn.execute(
                    """
                    SELECT id FROM session_events
                    WHERE session_id = ? AND idempotency_key = ?
                    """,
                    (session_id, idempotency_key),
                ).fetchone()
                if event:
                    event_row = conn.execute(
                        "SELECT payload_json FROM session_events WHERE id = ?",
                        (event["id"],),
                    ).fetchone()
                    assert event_row is not None
                    if (
                        _decoded(event_row["payload_json"], {}).get("state_digest")
                        != state_digest
                    ):
                        raise ContinuityError(
                            "idempotency key was reused with different checkpoint input",
                            code="idempotency_conflict",
                        )
                    prior = conn.execute(
                        "SELECT * FROM session_checkpoints WHERE source_event_id = ?",
                        (event["id"],),
                    ).fetchone()
                    if prior:
                        result = self._checkpoint_dict(prior)
                        assert result is not None
                        result["idempotent_replay"] = True
                        return result
            version = int(session["version"]) + 1
            event = self._insert_event(
                conn,
                session=session,
                agent_id=agent_id,
                event_type="checkpoint.created",
                payload={
                    "version": version,
                    "summary": summary,
                    "state_digest": state_digest,
                },
                idempotency_key=idempotency_key,
            )
            task_repo = conn.execute(
                """
                SELECT t.objective, r.id, r.name, r.canonical_path,
                       r.remote_url, r.default_branch
                FROM tasks t JOIN repositories r ON r.id = t.repository_id
                WHERE t.id = ?
                """,
                (session["task_id"],),
            ).fetchone()
            assert task_repo is not None
            repository = {
                "id": task_repo["id"],
                "name": task_repo["name"],
                "canonical_path": task_repo["canonical_path"],
                "remote_url": task_repo["remote_url"],
                "branch": task_repo["default_branch"],
            }
            checkpoint_id = _identifier("checkpoint")
            now = time.time()
            conn.execute(
                """
                INSERT INTO session_checkpoints (
                    id, session_id, version, objective, summary, completed_json,
                    remaining_json, constraints_json, approved_decisions_json,
                    proposed_decisions_json, open_questions_json, blockers_json,
                    important_files_json, known_failures_json, repository_json,
                    source_agent_id, source_event_id, source_sequence,
                    generated, created_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    checkpoint_id,
                    session_id,
                    version,
                    task_repo["objective"],
                    summary,
                    _json(fields["completed"]),
                    _json(fields["remaining"]),
                    _json(fields["constraints"]),
                    _json(fields["approved_decisions"]),
                    _json(fields["proposed_decisions"]),
                    _json(fields["open_questions"]),
                    _json(fields["blockers"]),
                    _json(fields["important_files"]),
                    _json(fields["known_failures"]),
                    _json(repository),
                    agent_id,
                    event["id"],
                    event["sequence"],
                    int(generated),
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE sessions SET version = ?, summary = ?, updated_at = ?
                WHERE id = ?
                """,
                (version, summary, now, session_id),
            )
            row = conn.execute(
                "SELECT * FROM session_checkpoints WHERE id = ?", (checkpoint_id,)
            ).fetchone()
            result = self._checkpoint_dict(row)
            assert result is not None
            result["idempotent_replay"] = False
            return result

    def _get_session(self, conn: sqlite3.Connection, session_id: str) -> dict[str, Any]:
        session = self._session_row(conn, session_id)
        result = dict(session)
        result["participants"] = [
            dict(row)
            for row in conn.execute(
                """
                SELECT p.*, a.name, a.system_type
                FROM session_participants p
                JOIN agents a ON a.id = p.agent_id
                WHERE p.session_id = ? ORDER BY p.joined_at, p.agent_id
                """,
                (session_id,),
            ).fetchall()
        ]
        latest = conn.execute(
            """
            SELECT * FROM session_checkpoints
            WHERE session_id = ? ORDER BY version DESC LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        result["checkpoint"] = self._checkpoint_dict(latest)
        return result

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self.store.connect() as conn:
            return self._get_session(conn, session_id)

    def get_checkpoint(
        self, session_id: str, version: int | None = None
    ) -> dict[str, Any] | None:
        with self.store.connect() as conn:
            self._session_row(conn, session_id)
            if version is None:
                row = conn.execute(
                    """
                    SELECT * FROM session_checkpoints
                    WHERE session_id = ? ORDER BY version DESC LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT * FROM session_checkpoints
                    WHERE session_id = ? AND version = ?
                    """,
                    (session_id, version),
                ).fetchone()
            return self._checkpoint_dict(row)

    @staticmethod
    def _added(previous: Sequence[object], current: Sequence[object]) -> list[object]:
        previous_keys = {_json(item) for item in previous}
        return [item for item in current if _json(item) not in previous_keys]

    def get_delta(
        self,
        session_id: str,
        *,
        known_version: int,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        if known_version < 0:
            raise ContinuityError("known_version must be zero or greater")
        with self.store.connect() as conn:
            session = self._session_row(conn, session_id)
            current = int(session["version"])
            if known_version > current:
                raise ContinuityError(
                    "known_version is newer than the session",
                    code="invalid_version",
                )
            if (
                agent_id is not None
                and self._participant(conn, session_id, agent_id) is None
            ):
                raise ContinuityError(
                    "agent is not a session participant", code="not_found"
                )
            base = None
            if known_version:
                base = conn.execute(
                    """
                    SELECT * FROM session_checkpoints
                    WHERE session_id = ? AND version = ?
                    """,
                    (session_id, known_version),
                ).fetchone()
                if base is None:
                    raise ContinuityError(
                        "checkpoint version is no longer available",
                        code="version_too_old",
                    )
            base_sequence = int(base["source_sequence"]) if base else 0
            events = [
                self._event_dict(row)
                for row in conn.execute(
                    """
                    SELECT * FROM session_events
                    WHERE session_id = ? AND sequence > ?
                    ORDER BY sequence
                    """,
                    (session_id, base_sequence),
                ).fetchall()
            ]
            latest = conn.execute(
                """
                SELECT * FROM session_checkpoints
                WHERE session_id = ? ORDER BY version DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            old_checkpoint = self._checkpoint_dict(base) or {}
            new_checkpoint = self._checkpoint_dict(latest)
            old_completed = old_checkpoint.get("completed", [])
            old_decisions = old_checkpoint.get(
                "approved_decisions", []
            ) + old_checkpoint.get("proposed_decisions", [])
            new_completed = (
                new_checkpoint.get("completed", []) if new_checkpoint else []
            )
            new_decisions = (
                new_checkpoint.get("approved_decisions", [])
                + new_checkpoint.get("proposed_decisions", [])
                if new_checkpoint
                else []
            )
            new_remaining = (
                new_checkpoint.get("remaining", []) if new_checkpoint else []
            )
            old_remaining = old_checkpoint.get("remaining", [])
            no_change = not events
            return {
                "session_id": session_id,
                "from_version": known_version,
                "to_version": current,
                "no_change": no_change,
                "events": events,
                "checkpoint": new_checkpoint if current > known_version else None,
                "completed_added": self._added(old_completed, new_completed),
                "remaining_changed": new_remaining
                if new_remaining != old_remaining
                else [],
                "new_decisions": self._added(old_decisions, new_decisions),
                "new_blockers": self._added(
                    old_checkpoint.get("blockers", []),
                    new_checkpoint.get("blockers", []) if new_checkpoint else [],
                ),
                "new_open_questions": self._added(
                    old_checkpoint.get("open_questions", []),
                    new_checkpoint.get("open_questions", []) if new_checkpoint else [],
                ),
                "retention_floor_version": 0,
            }

    def close_session(
        self,
        session_id: str,
        *,
        agent_id: str,
        summary: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        with self.store.connect(immediate=True) as conn:
            session = self._session_row(conn, session_id)
            if self._participant(conn, session_id, agent_id) is None:
                raise ContinuityError(
                    "agent is not a session participant", code="not_found"
                )
            if session["status"] == "closed":
                result = self._get_session(conn, session_id)
                result["idempotent_replay"] = True
                return result
            event = self._insert_event(
                conn,
                session=session,
                agent_id=agent_id,
                event_type="session.closed",
                payload={"summary": summary or session["summary"]},
                idempotency_key=idempotency_key,
            )
            now = time.time()
            conn.execute(
                """
                UPDATE sessions
                SET status = 'closed', closed_at = ?, updated_at = ?, summary = ?
                WHERE id = ?
                """,
                (now, now, summary or session["summary"], session_id),
            )
            result = self._get_session(conn, session_id)
            result["close_event"] = event
            result["idempotent_replay"] = False
            return result
