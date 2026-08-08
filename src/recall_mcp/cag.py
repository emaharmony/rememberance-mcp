"""Client-aware, scope-safe CAG delivery over canonical Recall artifacts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from recall_mcp.context import ContextPackRequest, ContextPackService, TokenEstimator
from recall_mcp.continuity import ContinuityError, SessionService, TaskService
from recall_mcp.handoff import HandoffScope, HandoffService
from recall_mcp.runtime import Settings
from recall_mcp.skills import SkillScope, SkillService
from recall_mcp.store.migrations import CURRENT_SCHEMA_VERSION, run_migrations


CAG_SCHEMA_VERSION = 1
CAG_POLICY_VERSION = "cag-v1"
DELIVERY_MODES = {
    "full",
    "delta",
    "no_change",
    "refresh_required",
    "fallback_full",
}
CAG_CAPABILITIES = {
    "structured_json",
    "markdown",
    "context_delta",
    "skill_delta",
    "skill_reference",
    "reference_expansion",
    "handoff_delta",
}


def _stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _identifier(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _as_version_map(name: str, value: Mapping[str, int] | None) -> dict[str, int]:
    result: dict[str, int] = {}
    for identifier, version in dict(value or {}).items():
        if not isinstance(identifier, str) or not identifier.strip():
            raise ContinuityError(f"{name} identifiers must be non-empty strings")
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise ContinuityError(f"{name} versions must be zero or greater")
        result[identifier] = version
    return result


@dataclass(frozen=True)
class ClientState:
    """Disposable client claims that are always verified against Recall."""

    client_id: str | None = None
    client_type: str | None = None
    known_checkpoint_version: int | None = None
    known_context_pack_id: str | None = None
    known_context_pack_fingerprint: str | None = None
    known_skills: Mapping[str, int] = field(default_factory=dict)
    known_handoffs: Mapping[str, int] = field(default_factory=dict)
    capabilities: tuple[str, ...] = ()

    def validated(self) -> ClientState:
        if (
            self.known_checkpoint_version is not None
            and self.known_checkpoint_version < 0
        ):
            raise ContinuityError("known_checkpoint_version must be zero or greater")
        if self.known_context_pack_fingerprint is not None and not str(
            self.known_context_pack_fingerprint
        ).startswith("sha256:"):
            raise ContinuityError(
                "known_context_pack_fingerprint must be a sha256 digest"
            )
        return replace(
            self,
            known_skills=_as_version_map("known_skills", self.known_skills),
            known_handoffs=_as_version_map("known_handoffs", self.known_handoffs),
            capabilities=tuple(sorted(set(self.capabilities))),
        )

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        return {key: item for key, item in value.items() if item not in (None, {}, ())}


@dataclass(frozen=True)
class CAGRequest:
    """Provider-neutral optimized-delivery request."""

    context: ContextPackRequest
    client_state: ClientState | None = None
    idempotency_key: str | None = None

    def validated(self, settings: Settings) -> CAGRequest:
        context = self.context.validated(settings)
        state = self.client_state.validated() if self.client_state is not None else None
        if state is not None and state.known_checkpoint_version is not None:
            if not context.session_id:
                raise ContinuityError("known checkpoint state requires a session_id")
        if self.idempotency_key is not None and not self.idempotency_key.strip():
            raise ContinuityError("idempotency_key must not be empty")
        return replace(self, context=context, client_state=state)


@dataclass
class _Metrics:
    deliveries_total: int = 0
    full_total: int = 0
    delta_total: int = 0
    no_change_total: int = 0
    refresh_required_total: int = 0
    fallback_total: int = 0
    cache_hits_total: int = 0
    cache_misses_total: int = 0
    cache_invalidations_total: int = 0
    cache_stale_total: int = 0
    cache_corrupt_total: int = 0
    skill_delta_total: int = 0
    context_delta_total: int = 0
    handoff_delta_total: int = 0
    estimated_tokens_full_total: int = 0
    estimated_tokens_delivered_total: int = 0
    estimated_tokens_avoided_total: int = 0
    delivery_latency_seconds_total: float = 0.0
    cache_lookup_latency_seconds_total: float = 0.0
    delta_generation_latency_seconds_total: float = 0.0
    telemetry_errors_total: int = 0
    last_error: str = ""
    last_invalidation_error: str = ""


class BoundedLRUCache:
    """Thread-safe JSON cache with deterministic least-recently-used eviction."""

    def __init__(self, max_entries: int, max_bytes: int, *, enabled: bool = True):
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.enabled = enabled and max_entries > 0 and max_bytes > 0
        self._items: OrderedDict[str, tuple[str, int]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.RLock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.corruptions = 0

    def get(self, key: str) -> object | None:
        if not self.enabled:
            self.misses += 1
            return None
        with self._lock:
            item = self._items.get(key)
            if item is None:
                self.misses += 1
                return None
            payload, size = item
            self._items.move_to_end(key)
            try:
                value = json.loads(payload)
            except (TypeError, ValueError):
                self._items.pop(key, None)
                self._bytes -= size
                self.corruptions += 1
                self.misses += 1
                return None
            self.hits += 1
            return value

    def put(self, key: str, value: object) -> bool:
        if not self.enabled:
            return False
        payload = _stable_json(value)
        size = len(payload.encode("utf-8"))
        if size > self.max_bytes:
            return False
        with self._lock:
            previous = self._items.pop(key, None)
            if previous is not None:
                self._bytes -= previous[1]
            self._items[key] = (payload, size)
            self._bytes += size
            while len(self._items) > self.max_entries or self._bytes > self.max_bytes:
                _evicted_key, (_payload, evicted_size) = self._items.popitem(last=False)
                self._bytes -= evicted_size
                self.evictions += 1
        return True

    def remove(self, key: str) -> bool:
        with self._lock:
            item = self._items.pop(key, None)
            if item is None:
                return False
            self._bytes -= item[1]
            return True

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._bytes = 0

    def inject_corrupt(self, key: str) -> None:
        """Test/diagnostic seam used to prove corruption is never authoritative."""
        if not self.enabled:
            return
        with self._lock:
            previous = self._items.pop(key, None)
            if previous is not None:
                self._bytes -= previous[1]
            self._items[key] = ("{corrupt", 8)
            self._bytes += 8

    def stats(self) -> dict[str, int | bool]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "entries": len(self._items),
                "bytes": self._bytes,
                "max_entries": self.max_entries,
                "max_bytes": self.max_bytes,
                "hits": self.hits,
                "misses": self.misses,
                "evictions": self.evictions,
                "corruptions": self.corruptions,
            }


class CAGDeliveryService:
    """Canonical application service for cache-aware Context Pack delivery."""

    def __init__(
        self,
        db_path: Path,
        settings: Settings,
        task_service: TaskService,
        session_service: SessionService,
        context_service: ContextPackService,
        skill_service: SkillService,
        handoff_service: HandoffService,
    ):
        self.db_path = Path(db_path)
        self.settings = settings
        self.task_service = task_service
        self.session_service = session_service
        self.context_service = context_service
        self.skill_service = skill_service
        self.handoff_service = handoff_service
        run_migrations(self.db_path)
        self.policy_version = settings.CAG_POLICY_VERSION or CAG_POLICY_VERSION
        self.estimator = TokenEstimator(settings.CONTEXT_TOKEN_ESTIMATOR_VERSION)
        self.hot_cache = BoundedLRUCache(
            settings.CACHE_MAX_ENTRIES,
            settings.CACHE_MAX_BYTES,
            enabled=settings.CAG_ENABLED,
        )
        self._metrics = _Metrics()
        self._metrics_lock = threading.Lock()

    @contextmanager
    def _connect(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        if immediate:
            conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            if immediate:
                conn.execute("COMMIT")
        except Exception:
            if immediate and conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @staticmethod
    def _scope(request: ContextPackRequest) -> dict[str, str | None]:
        return {
            "user_id": request.user_id,
            "workspace_id": request.workspace_id,
            "project_id": request.project_id,
            "repository_id": request.repository_id,
            "task_id": request.task_id,
            "session_id": request.session_id,
            "agent_id": request.agent_id,
        }

    def _resolve_scope(
        self, request: ContextPackRequest
    ) -> tuple[dict[str, str | None], dict[str, Any], dict[str, Any] | None]:
        scope = self._scope(request)
        task = self.task_service.get_task(
            str(request.task_id),
            user_id=request.user_id,
            project_id=request.project_id,
            repository_id=request.repository_id,
        )
        if task.get("workspace_id") != request.workspace_id:
            raise ContinuityError(
                "task is outside the requested scope", code="not_found"
            )
        session: dict[str, Any] | None = None
        if request.session_id:
            session = self.session_service.get_session(request.session_id)
            if session.get("task_id") != request.task_id:
                raise ContinuityError(
                    "session is outside the requested task", code="not_found"
                )
            participants = {
                str(item.get("agent_id")) for item in session.get("participants", [])
            }
            if request.agent_id not in participants:
                raise ContinuityError(
                    "agent is not a session participant", code="not_found"
                )
        return scope, task, session

    @staticmethod
    def _skill_scope(scope: Mapping[str, str | None]) -> SkillScope:
        return SkillScope(
            user_id=str(scope["user_id"]),
            workspace_id=str(scope["workspace_id"]),
            project_id=str(scope["project_id"]),
            repository_id=scope.get("repository_id"),
        )

    @staticmethod
    def _handoff_scope(scope: Mapping[str, str | None]) -> HandoffScope | None:
        if not scope.get("session_id"):
            return None
        return HandoffScope(
            user_id=str(scope["user_id"]),
            workspace_id=str(scope["workspace_id"]),
            project_id=str(scope["project_id"]),
            repository_id=str(scope["repository_id"]),
            task_id=str(scope["task_id"]),
            session_id=str(scope["session_id"]),
        )

    def _skills(
        self,
        scope: Mapping[str, str | None],
        *,
        objective: str,
        token_limit: int,
    ) -> list[dict[str, Any]]:
        return self.skill_service.select_for_context(
            scope=self._skill_scope(scope),
            objective=objective,
            token_limit=token_limit,
        )

    def _handoffs(self, scope: Mapping[str, str | None]) -> list[dict[str, Any]]:
        handoff_scope = self._handoff_scope(scope)
        if handoff_scope is None:
            return []
        rows = self.handoff_service.list_handoffs(
            scope=handoff_scope,
            agent_id=scope.get("agent_id"),
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            result.append(
                self.handoff_service.get(
                    str(row["id"]),
                    scope=handoff_scope,
                    version=int(row["current_version"]),
                    agent_id=scope.get("agent_id"),
                )
            )
        return result

    def cache_key(
        self,
        *,
        scope: Mapping[str, str | None],
        request: ContextPackRequest,
        task: Mapping[str, Any],
        session: Mapping[str, Any] | None,
        skills: Sequence[Mapping[str, Any]],
        handoffs: Sequence[Mapping[str, Any]],
        capabilities: Sequence[str] = (),
    ) -> str:
        """Build the only CAG cache key; no captured text is included."""
        material = {
            "scope": dict(scope),
            "request": {
                "max_tokens": request.max_tokens
                or self.settings.CONTEXT_DEFAULT_MAX_TOKENS,
                "requested_sections": sorted(request.requested_sections),
                "client_capabilities": sorted(request.client_capabilities),
                "branch": request.branch,
                "commit_sha": request.commit_sha,
                "retrieval_limit": request.retrieval_limit,
                "include_cold": request.include_cold,
            },
            "state": {
                "task_id": task.get("id"),
                "task_updated_at": task.get("updated_at"),
                "checkpoint_version": session.get("version") if session else None,
                "skills": {str(item["id"]): int(item["version"]) for item in skills},
                "handoffs": {
                    str(item["handoff_id"]): int(item["version"]) for item in handoffs
                },
            },
            "policy": {
                "context": self.context_service.policy_version,
                "cag": self.policy_version,
                "token_estimator": self.estimator.version,
                "utility": self.context_service.feedback_service.policy_version,
            },
        }
        return "cag:" + _digest(material)

    @staticmethod
    def _row_scope_values(scope: Mapping[str, str | None]) -> tuple[object, ...]:
        return tuple(
            scope.get(field)
            for field in (
                "user_id",
                "workspace_id",
                "project_id",
                "repository_id",
                "task_id",
                "session_id",
                "agent_id",
            )
        )

    @staticmethod
    def _scope_sql(prefix: str = "") -> str:
        return " AND ".join(
            f"{prefix}{field} IS ?"
            for field in (
                "user_id",
                "workspace_id",
                "project_id",
                "repository_id",
                "task_id",
                "session_id",
                "agent_id",
            )
        )

    @staticmethod
    def _task_hash(task: Mapping[str, object]) -> str:
        return _digest(
            {
                key: task.get(key)
                for key in ("id", "title", "objective", "status", "updated_at")
            }
        )

    @staticmethod
    def _session_hash(session: Mapping[str, object] | None) -> str:
        if session is None:
            return _digest(None)
        return _digest(
            {
                "id": session.get("id"),
                "status": session.get("status"),
                "version": session.get("version"),
                "event_sequence": session.get("event_sequence"),
                "updated_at": session.get("updated_at"),
                "participants": session.get("participants"),
                "checkpoint": session.get("checkpoint"),
            }
        )

    def _memory_scope_hash(
        self, conn: sqlite3.Connection, scope: Mapping[str, str | None]
    ) -> str:
        rows = conn.execute(
            """
            SELECT id, summary, content, created_at, lifecycle_state, pinned,
                   utility_policy_version
            FROM memories
            WHERE user_id = ? AND workspace_id = ? AND project_id = ?
              AND repository_id = ? AND (task_id IS NULL OR task_id = ?)
            ORDER BY id
            """,
            (
                scope["user_id"],
                scope["workspace_id"],
                scope["project_id"],
                scope["repository_id"],
                scope["task_id"],
            ),
        ).fetchall()
        return _digest([dict(row) for row in rows])

    def _dependencies(
        self,
        *,
        scope: Mapping[str, str | None],
        request: ContextPackRequest,
        task: Mapping[str, Any],
        session: Mapping[str, Any] | None,
        skills: Sequence[Mapping[str, Any]],
        handoffs: Sequence[Mapping[str, Any]],
        pack: Mapping[str, Any],
    ) -> list[dict[str, str | None]]:
        now = time.time()
        checkpoint = session.get("checkpoint") if session else None
        dependencies: list[dict[str, str | None]] = [
            {
                "dependency_type": "task",
                "dependency_id": str(task["id"]),
                "dependency_version": str(task.get("updated_at")),
                "dependency_hash": self._task_hash(task),
                "created_at": str(now),
            },
            {
                "dependency_type": "session_checkpoint",
                "dependency_id": str(scope.get("session_id") or "none"),
                "dependency_version": str(session.get("version") if session else 0),
                "dependency_hash": self._session_hash(session),
                "created_at": str(now),
            },
            {
                "dependency_type": "decision",
                "dependency_id": str(scope.get("session_id") or task["id"]),
                "dependency_version": str(session.get("version") if session else 0),
                "dependency_hash": _digest(
                    {
                        "approved": (checkpoint or {}).get("approved_decisions", []),
                        "proposed": (checkpoint or {}).get("proposed_decisions", []),
                    }
                ),
                "created_at": str(now),
            },
            {
                "dependency_type": "constraint",
                "dependency_id": str(scope.get("session_id") or task["id"]),
                "dependency_version": str(session.get("version") if session else 0),
                "dependency_hash": _digest((checkpoint or {}).get("constraints", [])),
                "created_at": str(now),
            },
            {
                "dependency_type": "validation_state",
                "dependency_id": str(scope.get("session_id") or task["id"]),
                "dependency_version": str(session.get("version") if session else 0),
                "dependency_hash": _digest(pack.get("validation_requests", [])),
                "created_at": str(now),
            },
            {
                "dependency_type": "repository_commit",
                "dependency_id": str(scope["repository_id"]),
                "dependency_version": request.commit_sha,
                "dependency_hash": _digest(
                    {"branch": request.branch, "commit_sha": request.commit_sha}
                ),
                "created_at": str(now),
            },
            {
                "dependency_type": "policy",
                "dependency_id": "context",
                "dependency_version": self.context_service.policy_version,
                "dependency_hash": _digest(self.context_service.policy_version),
                "created_at": str(now),
            },
            {
                "dependency_type": "policy",
                "dependency_id": "cag",
                "dependency_version": self.policy_version,
                "dependency_hash": _digest(self.policy_version),
                "created_at": str(now),
            },
            {
                "dependency_type": "token_estimator",
                "dependency_id": "context",
                "dependency_version": self.estimator.version,
                "dependency_hash": _digest(self.estimator.version),
                "created_at": str(now),
            },
        ]
        with self._connect() as conn:
            dependencies.append(
                {
                    "dependency_type": "memory",
                    "dependency_id": "scope",
                    "dependency_version": None,
                    "dependency_hash": self._memory_scope_hash(conn, scope),
                    "created_at": str(now),
                }
            )
        for skill in skills:
            dependencies.append(
                {
                    "dependency_type": "skill",
                    "dependency_id": str(skill["id"]),
                    "dependency_version": str(skill["version"]),
                    "dependency_hash": _digest(
                        {
                            "content_hash": skill.get("content_hash"),
                            "source_fingerprint": skill.get("source_fingerprint"),
                            "status": skill.get("status"),
                        }
                    ),
                    "created_at": str(now),
                }
            )
        for handoff in handoffs:
            dependencies.append(
                {
                    "dependency_type": "handoff",
                    "dependency_id": str(handoff["handoff_id"]),
                    "dependency_version": str(handoff["version"]),
                    "dependency_hash": _digest(
                        {
                            "content_hash": handoff.get("content_hash"),
                            "source_fingerprint": handoff.get("source_fingerprint"),
                            "status": handoff.get("status"),
                        }
                    ),
                    "created_at": str(now),
                }
            )
        dependencies.sort(
            key=lambda item: (str(item["dependency_type"]), str(item["dependency_id"]))
        )
        return dependencies

    @staticmethod
    def _dependency_fingerprint(
        dependencies: Sequence[Mapping[str, object]],
    ) -> str:
        return _digest(
            [
                {
                    "type": item["dependency_type"],
                    "id": item["dependency_id"],
                    "version": item.get("dependency_version"),
                    "hash": item["dependency_hash"],
                }
                for item in dependencies
            ]
        )

    def _current_dependency_hash(
        self,
        conn: sqlite3.Connection,
        dependency: sqlite3.Row,
        scope: Mapping[str, str | None],
        request: ContextPackRequest,
    ) -> str | None:
        kind = str(dependency["dependency_type"])
        identifier = str(dependency["dependency_id"])
        if kind == "task":
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (identifier,)
            ).fetchone()
            return self._task_hash(dict(row)) if row else None
        if kind in {"session_checkpoint", "decision", "constraint", "validation_state"}:
            if not scope.get("session_id"):
                if kind == "session_checkpoint":
                    return _digest(None)
                if kind == "decision":
                    return _digest({"approved": [], "proposed": []})
                return _digest([])
            session = self.session_service.get_session(str(scope["session_id"]))
            checkpoint = session.get("checkpoint") or {}
            if kind == "session_checkpoint":
                return self._session_hash(session)
            if kind == "decision":
                return _digest(
                    {
                        "approved": checkpoint.get("approved_decisions", []),
                        "proposed": checkpoint.get("proposed_decisions", []),
                    }
                )
            if kind == "constraint":
                return _digest(checkpoint.get("constraints", []))
            # Validation requests are derived from the same checkpoint and scoped
            # evidence; the session and memory dependencies are authoritative.
            return str(dependency["dependency_hash"])
        if kind == "memory":
            if identifier == "scope":
                return self._memory_scope_hash(conn, scope)
            row = conn.execute(
                "SELECT id, summary, content, created_at, lifecycle_state, pinned "
                "FROM memories WHERE id = ?",
                (identifier,),
            ).fetchone()
            return _digest(dict(row)) if row else None
        if kind == "skill":
            row = conn.execute(
                """
                SELECT s.status, s.stale_at, s.current_approved_version,
                       v.content_hash, v.source_fingerprint
                FROM skills s LEFT JOIN skill_versions v
                  ON v.skill_id = s.id AND v.version = s.current_approved_version
                WHERE s.id = ?
                """,
                (identifier,),
            ).fetchone()
            if row is None or row["stale_at"] is not None:
                return None
            return _digest(
                {
                    "content_hash": row["content_hash"],
                    "source_fingerprint": row["source_fingerprint"],
                    "status": row["status"],
                }
            )
        if kind == "handoff":
            row = conn.execute(
                """
                SELECT h.status, h.current_version, v.content_hash,
                       v.source_fingerprint
                FROM handoffs h JOIN handoff_versions v
                  ON v.handoff_id = h.id AND v.version = h.current_version
                WHERE h.id = ?
                """,
                (identifier,),
            ).fetchone()
            if row is None:
                return None
            return _digest(
                {
                    "content_hash": row["content_hash"],
                    "source_fingerprint": row["source_fingerprint"],
                    "status": row["status"],
                }
            )
        if kind == "repository_commit":
            return _digest({"branch": request.branch, "commit_sha": request.commit_sha})
        if kind == "policy":
            value = (
                self.context_service.policy_version
                if identifier == "context"
                else self.policy_version
            )
            return _digest(value)
        if kind == "token_estimator":
            return _digest(self.estimator.version)
        return None

    def _mark_entry(
        self,
        entry: sqlite3.Row,
        *,
        status: str,
        reason: str,
        actor_id: str | None = None,
    ) -> None:
        now = time.time()
        try:
            with self._connect(immediate=True) as conn:
                conn.execute(
                    """
                    UPDATE cag_cache_entries
                    SET status = ?, stale_at = ?, stale_reason = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status, now, reason, now, entry["id"]),
                )
                conn.execute(
                    """
                    INSERT INTO cag_invalidation_events (
                        id, cache_entry_id, user_id, workspace_id, project_id,
                        repository_id, task_id, session_id, actor_id, reason,
                        previous_status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _identifier("invalidation"),
                        entry["id"],
                        entry["user_id"],
                        entry["workspace_id"],
                        entry["project_id"],
                        entry["repository_id"],
                        entry["task_id"],
                        entry["session_id"],
                        actor_id,
                        reason,
                        entry["status"],
                        now,
                    ),
                )
            self.hot_cache.remove(str(entry["cache_key"]))
            with self._metrics_lock:
                self._metrics.cache_invalidations_total += 1
                if status in {"stale", "expired"}:
                    self._metrics.cache_stale_total += 1
        except Exception as exc:
            with self._metrics_lock:
                self._metrics.last_invalidation_error = f"{type(exc).__name__}: {exc}"
            raise

    def _entry_valid(
        self,
        entry: sqlite3.Row,
        *,
        scope: Mapping[str, str | None],
        request: ContextPackRequest,
    ) -> tuple[bool, str | None]:
        now = time.time()
        if entry["status"] != "fresh":
            return False, str(entry["stale_reason"] or entry["status"])
        if entry["expires_at"] is not None and float(entry["expires_at"]) <= now:
            self._mark_entry(entry, status="expired", reason="ttl_expired")
            return False, "ttl_expired"
        if entry["policy_version"] != self.policy_version:
            self._mark_entry(entry, status="invalid", reason="policy_changed")
            return False, "policy_changed"
        with self._connect() as conn:
            dependencies = conn.execute(
                """
                SELECT * FROM cag_cache_dependencies
                WHERE cache_entry_id = ?
                ORDER BY dependency_type, dependency_id
                """,
                (entry["id"],),
            ).fetchall()
            if not dependencies:
                self._mark_entry(entry, status="invalid", reason="missing_dependencies")
                return False, "missing_dependencies"
            current = []
            for dependency in dependencies:
                current_hash = self._current_dependency_hash(
                    conn, dependency, scope, request
                )
                if (
                    current_hash is None
                    or current_hash != dependency["dependency_hash"]
                ):
                    self._mark_entry(entry, status="stale", reason="source_changed")
                    return False, "source_changed"
                current.append(
                    {
                        "type": dependency["dependency_type"],
                        "id": dependency["dependency_id"],
                        "version": dependency["dependency_version"],
                        "hash": current_hash,
                    }
                )
        if _digest(current) != entry["dependency_fingerprint"]:
            self._mark_entry(entry, status="invalid", reason="dependency_mismatch")
            return False, "dependency_mismatch"
        return True, None

    def _entry_by_key(
        self, cache_key: str, scope: Mapping[str, str | None]
    ) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                f"SELECT * FROM cag_cache_entries WHERE cache_key = ? AND {self._scope_sql()}",
                (cache_key, *self._row_scope_values(scope)),
            ).fetchone()

    def _entry_for_pack(
        self, pack_id: str, scope: Mapping[str, str | None]
    ) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                f"""
                SELECT * FROM cag_cache_entries
                WHERE artifact_type = 'context_pack' AND artifact_id = ?
                  AND {self._scope_sql()}
                ORDER BY created_at DESC LIMIT 1
                """,
                (pack_id, *self._row_scope_values(scope)),
            ).fetchone()

    def _touch_entry(self, entry: sqlite3.Row) -> None:
        now = time.time()
        with self._connect(immediate=True) as conn:
            conn.execute(
                """
                UPDATE cag_cache_entries
                SET last_accessed_at = ?, access_count = access_count + 1,
                    updated_at = ? WHERE id = ?
                """,
                (now, now, entry["id"]),
            )

    def _load_entry_pack(
        self,
        entry: sqlite3.Row,
        scope: Mapping[str, str | None],
    ) -> tuple[dict[str, Any], bool]:
        cached = self.hot_cache.get(str(entry["cache_key"]))
        if isinstance(cached, dict):
            if _digest(cached) == entry["content_hash"]:
                self._touch_entry(entry)
                return cached, True
            with self._metrics_lock:
                self._metrics.cache_corrupt_total += 1
            self.hot_cache.remove(str(entry["cache_key"]))
        pack = self.context_service.get(str(entry["artifact_id"]), scope)
        if _digest(pack) != entry["content_hash"]:
            self._mark_entry(entry, status="invalid", reason="content_hash_mismatch")
            raise ContinuityError(
                "cached context pack is corrupt", code="cache_corrupt"
            )
        self.hot_cache.put(str(entry["cache_key"]), pack)
        self._touch_entry(entry)
        return pack, False

    def _store_entry(
        self,
        *,
        cache_key: str,
        scope: Mapping[str, str | None],
        request: ContextPackRequest,
        task: Mapping[str, Any],
        session: Mapping[str, Any] | None,
        skills: Sequence[Mapping[str, Any]],
        handoffs: Sequence[Mapping[str, Any]],
        pack: Mapping[str, Any],
    ) -> sqlite3.Row:
        dependencies = self._dependencies(
            scope=scope,
            request=request,
            task=task,
            session=session,
            skills=skills,
            handoffs=handoffs,
            pack=pack,
        )
        dependency_fingerprint = self._dependency_fingerprint(dependencies)
        now = time.time()
        entry_id = _identifier("cache")
        expires_at = min(
            float(
                pack.get("freshness", {}).get(
                    "expires_at", now + self.settings.CACHE_TTL_SECONDS
                )
            ),
            now + self.settings.CACHE_TTL_SECONDS,
        )
        with self._connect(immediate=True) as conn:
            prior = conn.execute(
                "SELECT * FROM cag_cache_entries WHERE cache_key = ?", (cache_key,)
            ).fetchone()
            if prior is not None:
                entry_id = str(prior["id"])
                conn.execute(
                    "DELETE FROM cag_cache_dependencies WHERE cache_entry_id = ?",
                    (entry_id,),
                )
                conn.execute(
                    """
                    UPDATE cag_cache_entries SET artifact_id = ?, artifact_version = ?,
                        schema_version = ?, policy_version = ?, source_fingerprint = ?,
                        dependency_fingerprint = ?, content_hash = ?, token_estimate = ?,
                        status = 'fresh', metadata_json = ?, updated_at = ?,
                        last_accessed_at = ?, expires_at = ?, stale_at = NULL,
                        stale_reason = NULL, access_count = 0
                    WHERE id = ?
                    """,
                    (
                        pack["context_pack_id"],
                        str(pack.get("schema_version")),
                        CAG_SCHEMA_VERSION,
                        self.policy_version,
                        pack["freshness"]["source_fingerprint"],
                        dependency_fingerprint,
                        _digest(pack),
                        int(pack.get("token_usage", {}).get("estimated_total", 0)),
                        _stable_json({"context_policy": pack.get("policy_version")}),
                        now,
                        now,
                        expires_at,
                        entry_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO cag_cache_entries (
                        id, cache_key, artifact_type, user_id, workspace_id,
                        project_id, repository_id, task_id, session_id, agent_id,
                        artifact_id, artifact_version, schema_version, policy_version,
                        source_fingerprint, dependency_fingerprint, content_hash,
                        token_estimate, status, metadata_json, created_at, updated_at,
                        last_accessed_at, expires_at, access_count
                    ) VALUES (?, ?, 'context_pack', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              ?, ?, ?, 'fresh', ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        entry_id,
                        cache_key,
                        scope["user_id"],
                        scope["workspace_id"],
                        scope["project_id"],
                        scope["repository_id"],
                        scope["task_id"],
                        scope["session_id"],
                        scope["agent_id"],
                        pack["context_pack_id"],
                        str(pack.get("schema_version")),
                        CAG_SCHEMA_VERSION,
                        self.policy_version,
                        pack["freshness"]["source_fingerprint"],
                        dependency_fingerprint,
                        _digest(pack),
                        int(pack.get("token_usage", {}).get("estimated_total", 0)),
                        _stable_json({"context_policy": pack.get("policy_version")}),
                        now,
                        now,
                        now,
                        expires_at,
                    ),
                )
            for dependency in dependencies:
                conn.execute(
                    """
                    INSERT INTO cag_cache_dependencies (
                        cache_entry_id, dependency_type, dependency_id,
                        dependency_version, dependency_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry_id,
                        dependency["dependency_type"],
                        dependency["dependency_id"],
                        dependency["dependency_version"],
                        dependency["dependency_hash"],
                        float(str(dependency["created_at"])),
                    ),
                )
            row = conn.execute(
                "SELECT * FROM cag_cache_entries WHERE id = ?", (entry_id,)
            ).fetchone()
            assert row is not None
        self.hot_cache.put(cache_key, pack)
        return row

    @staticmethod
    def _list_delta(
        previous: Sequence[object], current: Sequence[object]
    ) -> dict[str, list[object]]:
        old = {_stable_json(item): item for item in previous}
        new = {_stable_json(item): item for item in current}
        return {
            "added": [new[key] for key in new.keys() - old.keys()],
            "removed": [old[key] for key in old.keys() - new.keys()],
        }

    def skill_delta(
        self,
        skill_id: str,
        *,
        from_version: int,
        to_version: int,
        scope: SkillScope,
    ) -> dict[str, Any]:
        previous = self.skill_service.get(skill_id, scope=scope, version=from_version)
        current = self.skill_service.get(skill_id, scope=scope, version=to_version)
        if previous["schema_version"] != current["schema_version"]:
            return {
                "skill_id": skill_id,
                "from_version": from_version,
                "to_version": to_version,
                "requires_full_refresh": True,
                "reason": "schema_changed",
                "skill": current,
            }
        old_content = previous.get("content", {})
        new_content = current.get("content", {})
        changes: dict[str, object] = {
            "summary_changed": old_content.get("summary") != new_content.get("summary")
        }
        for field_name in (
            "instructions",
            "facts",
            "decisions",
            "constraints",
            "open_questions",
            "evidence",
        ):
            delta = self._list_delta(
                list(old_content.get(field_name) or []),
                list(new_content.get(field_name) or []),
            )
            changes[f"{field_name}_added"] = delta["added"]
            changes[f"{field_name}_removed"] = delta["removed"]
        result = {
            "skill_id": skill_id,
            "from_version": from_version,
            "to_version": to_version,
            "changes": changes,
            "requires_full_refresh": False,
        }
        delta_tokens = self.estimator.estimate(result)
        full_tokens = max(1, int(current["token_estimate"]))
        if delta_tokens > full_tokens * self.settings.SKILL_DELTA_MAX_RATIO:
            return {
                "skill_id": skill_id,
                "from_version": from_version,
                "to_version": to_version,
                "requires_full_refresh": True,
                "reason": "delta_larger_than_threshold",
                "skill": current,
            }
        return result

    def _classify_skills(
        self,
        *,
        current: Sequence[Mapping[str, Any]],
        state: ClientState | None,
        scope: Mapping[str, str | None],
    ) -> dict[str, list[dict[str, Any]]]:
        known = dict(state.known_skills) if state else {}
        capabilities = set(state.capabilities) if state else set()
        skill_scope = self._skill_scope(scope)
        current_map = {str(item["id"]): item for item in current}
        result: dict[str, list[dict[str, Any]]] = {
            "unchanged": [],
            "updated": [],
            "missing": [],
            "stale_client_versions": [],
            "removed": [],
        }
        for skill_id, item in current_map.items():
            version = int(item["version"])
            known_version = known.get(skill_id)
            if known_version is None:
                full = self.skill_service.get(
                    skill_id, scope=skill_scope, version=version
                )
                result["missing"].append(
                    {
                        "id": skill_id,
                        "version": version,
                        "delivery": "full",
                        "skill": full,
                    }
                )
            elif known_version == version:
                result["unchanged"].append({"id": skill_id, "version": version})
            elif known_version < version:
                if "skill_delta" in capabilities:
                    try:
                        delta = self.skill_delta(
                            skill_id,
                            from_version=known_version,
                            to_version=version,
                            scope=skill_scope,
                        )
                    except ContinuityError:
                        delta = {
                            "requires_full_refresh": True,
                            "reason": "unknown_client_version",
                            "skill": self.skill_service.get(
                                skill_id, scope=skill_scope, version=version
                            ),
                        }
                    result["updated"].append(
                        {
                            "id": skill_id,
                            "from_version": known_version,
                            "to_version": version,
                            "delivery": "full"
                            if delta.get("requires_full_refresh")
                            else "delta",
                            **delta,
                        }
                    )
                else:
                    result["updated"].append(
                        {
                            "id": skill_id,
                            "from_version": known_version,
                            "to_version": version,
                            "delivery": "full",
                            "skill": self.skill_service.get(
                                skill_id, scope=skill_scope, version=version
                            ),
                        }
                    )
            else:
                result["stale_client_versions"].append(
                    {
                        "id": skill_id,
                        "client_version": known_version,
                        "current_version": version,
                        "reason": "future_or_unknown_version",
                    }
                )
        for skill_id, known_version in known.items():
            if skill_id in current_map:
                continue
            try:
                held = self.skill_service.get(
                    skill_id, scope=skill_scope, version=known_version
                )
            except ContinuityError:
                # Do not reveal whether an unknown identifier exists out of scope.
                continue
            if held.get("stale_at") is not None or held.get("status") in {
                "stale",
                "deprecated",
                "archived",
            }:
                result["stale_client_versions"].append(
                    {
                        "id": skill_id,
                        "client_version": known_version,
                        "reason": held.get("stale_reason") or held.get("status"),
                    }
                )
            else:
                result["removed"].append(
                    {"id": skill_id, "version": known_version, "reason": "not_relevant"}
                )
        return result

    def _classify_handoffs(
        self,
        *,
        current: Sequence[Mapping[str, Any]],
        state: ClientState | None,
        scope: Mapping[str, str | None],
    ) -> dict[str, list[dict[str, Any]]]:
        known = dict(state.known_handoffs) if state else {}
        capabilities = set(state.capabilities) if state else set()
        known_checkpoint_version = (
            state.known_checkpoint_version if state is not None else None
        )
        result: dict[str, list[dict[str, Any]]] = {
            "unchanged": [],
            "updated": [],
            "missing": [],
            "refresh_required": [],
        }
        handoff_scope = self._handoff_scope(scope)
        if handoff_scope is None:
            return result
        for handoff in current:
            handoff_id = str(handoff["handoff_id"])
            version = int(handoff["version"])
            known_version = known.get(handoff_id)
            if handoff.get("status") in {"expired", "cancelled", "superseded"}:
                if known_version is not None:
                    result["refresh_required"].append(
                        {
                            "id": handoff_id,
                            "version": version,
                            "status": handoff.get("status"),
                        }
                    )
                continue
            if known_version is None:
                result["missing"].append(
                    {"id": handoff_id, "version": version, "handoff": handoff}
                )
            elif known_version == version:
                result["unchanged"].append({"id": handoff_id, "version": version})
            elif known_version < version and "handoff_delta" in capabilities:
                result["updated"].append(
                    self.handoff_service.get_delta(
                        handoff_id,
                        scope=handoff_scope,
                        known_version=known_version,
                        agent_id=scope.get("agent_id"),
                        known_checkpoint_version=known_checkpoint_version,
                    )
                )
            else:
                result["refresh_required"].append(
                    {
                        "id": handoff_id,
                        "client_version": known_version,
                        "current_version": version,
                        "reason": "invalid_or_unsupported_delta",
                    }
                )
        return result

    def context_delta(
        self,
        previous: Mapping[str, Any],
        current: Mapping[str, Any],
        *,
        known_checkpoint_version: int | None,
        agent_id: str | None,
    ) -> dict[str, Any]:
        previous_work = previous.get("work_state") or {}
        current_work = current.get("work_state") or {}
        previous_decisions = previous.get("decisions") or {}
        current_decisions = current.get("decisions") or {}
        previous_constraints = previous.get("constraints") or {}
        current_constraints = current.get("constraints") or {}
        session_delta = None
        current_session = current.get("session") or {}
        if known_checkpoint_version is not None and current_session.get("id"):
            same_pack = previous.get("context_pack_id") == current.get(
                "context_pack_id"
            )
            current_checkpoint = current_session.get("latest_checkpoint_version")
            if same_pack and known_checkpoint_version == current_checkpoint:
                session_delta = {
                    "session_id": current_session["id"],
                    "from_version": known_checkpoint_version,
                    "to_version": current_checkpoint,
                    "no_change": True,
                    "events": [],
                    "checkpoint": None,
                }
            else:
                session_delta = self.session_service.get_delta(
                    str(current_session["id"]),
                    known_version=known_checkpoint_version,
                    agent_id=agent_id,
                )
        result: dict[str, Any] = {
            "from_context_pack_id": previous.get("context_pack_id"),
            "to_context_pack_id": current.get("context_pack_id"),
            "task": {
                "changed": previous.get("active_task") != current.get("active_task"),
                **(
                    {"current": current.get("active_task")}
                    if previous.get("active_task") != current.get("active_task")
                    else {}
                ),
            },
            "session": {
                "from_checkpoint": known_checkpoint_version,
                "to_checkpoint": current_session.get("latest_checkpoint_version"),
                "delta": session_delta,
            },
            "constraints": {
                key: self._list_delta(
                    list(previous_constraints.get(key) or []),
                    list(current_constraints.get(key) or []),
                )
                for key in ("critical", "normal")
            },
            "decisions": {
                key: self._list_delta(
                    list(previous_decisions.get(key) or []),
                    list(current_decisions.get(key) or []),
                )
                for key in ("approved", "proposed")
            },
            "work_state": {
                key: self._list_delta(
                    list(previous_work.get(key) or []),
                    list(current_work.get(key) or []),
                )
                for key in (
                    "completed",
                    "remaining",
                    "blockers",
                    "open_questions",
                    "important_files",
                    "known_failures",
                )
            },
            "validation_requests": self._list_delta(
                list(previous.get("validation_requests") or []),
                list(current.get("validation_requests") or []),
            ),
            "warnings": self._list_delta(
                list(previous.get("warnings") or []),
                list(current.get("warnings") or []),
            ),
            "retrieval": self._list_delta(
                list(
                    (previous.get("retrieval") or {}).get("selected_memory_ids") or []
                ),
                list((current.get("retrieval") or {}).get("selected_memory_ids") or []),
            ),
        }
        result["changed"] = any(
            (
                result["task"]["changed"],
                session_delta is not None and not session_delta.get("no_change", False),
                any(
                    delta[part]
                    for section in ("constraints", "decisions", "work_state")
                    for delta in result[section].values()
                    for part in ("added", "removed")
                ),
                any(result["validation_requests"].values()),
                any(result["warnings"].values()),
                any(result["retrieval"].values()),
            )
        )
        return result

    def _authoritative_state(
        self,
        *,
        pack: Mapping[str, Any],
        session: Mapping[str, Any] | None,
        skills: Sequence[Mapping[str, Any]],
        handoffs: Sequence[Mapping[str, Any]],
    ) -> dict[str, object]:
        return {
            "checkpoint_version": int(session.get("version", 0)) if session else 0,
            "context_pack_id": pack["context_pack_id"],
            "context_pack_fingerprint": pack["freshness"]["source_fingerprint"],
            "skills": {str(item["id"]): int(item["version"]) for item in skills},
            "handoffs": {
                str(item["handoff_id"]): int(item["version"]) for item in handoffs
            },
        }

    def _build_pack(self, request: ContextPackRequest) -> dict[str, Any]:
        canonical_request = replace(
            request,
            known_checkpoint_version=None,
            known_context_pack_id=None,
            idempotency_key=None,
        )
        return self.context_service.build(canonical_request)

    def _current_pack(
        self,
        *,
        cache_key: str,
        scope: Mapping[str, str | None],
        request: ContextPackRequest,
        task: Mapping[str, Any],
        session: Mapping[str, Any] | None,
        skills: Sequence[Mapping[str, Any]],
        handoffs: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, Any], sqlite3.Row | None, bool, bool, str | None]:
        lookup_started = time.perf_counter()
        entry: sqlite3.Row | None = None
        fallback_reason: str | None = None
        if self.settings.CAG_ENABLED:
            try:
                entry = self._entry_by_key(cache_key, scope)
                if entry is not None:
                    valid, reason = self._entry_valid(
                        entry, scope=scope, request=request
                    )
                    if valid:
                        pack, hot_hit = self._load_entry_pack(entry, scope)
                        with self._metrics_lock:
                            self._metrics.cache_hits_total += 1
                            self._metrics.cache_lookup_latency_seconds_total += (
                                time.perf_counter() - lookup_started
                            )
                        return pack, entry, True, hot_hit, None
                    fallback_reason = reason
            except Exception as exc:
                fallback_reason = f"cache_lookup_failed:{type(exc).__name__}"
        with self._metrics_lock:
            self._metrics.cache_misses_total += 1
            self._metrics.cache_lookup_latency_seconds_total += (
                time.perf_counter() - lookup_started
            )
        pack = self._build_pack(request)
        if self.settings.CAG_ENABLED:
            try:
                entry = self._store_entry(
                    cache_key=cache_key,
                    scope=scope,
                    request=request,
                    task=task,
                    session=session,
                    skills=skills,
                    handoffs=handoffs,
                    pack=pack,
                )
            except Exception as exc:
                fallback_reason = (
                    fallback_reason or f"cache_write_failed:{type(exc).__name__}"
                )
                with self._metrics_lock:
                    self._metrics.telemetry_errors_total += 1
                    self._metrics.last_error = fallback_reason
        return pack, entry, False, False, fallback_reason

    def _client_pack(
        self,
        state: ClientState,
        scope: Mapping[str, str | None],
        request: ContextPackRequest,
    ) -> tuple[dict[str, Any] | None, str | None]:
        if not state.known_context_pack_id:
            return None, None
        try:
            pack = self.context_service.get(state.known_context_pack_id, scope)
        except Exception:
            return None, "unknown_context_pack"
        fingerprint = pack.get("freshness", {}).get("source_fingerprint")
        if (
            state.known_context_pack_fingerprint is not None
            and state.known_context_pack_fingerprint != fingerprint
        ):
            return None, "context_pack_fingerprint_mismatch"
        entry = self._entry_for_pack(state.known_context_pack_id, scope)
        if entry is None:
            return pack, "unverifiable_context_pack"
        valid, reason = self._entry_valid(entry, scope=scope, request=request)
        if not valid and reason not in {"source_changed", "ttl_expired"}:
            return pack, reason or "invalid_context_pack"
        return pack, None

    def _idempotent_replay(
        self,
        request: CAGRequest,
        scope: Mapping[str, str | None],
    ) -> dict[str, Any] | None:
        state = request.client_state
        if not request.idempotency_key or state is None or not state.client_id:
            return None
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM cag_deliveries
                WHERE user_id = ? AND client_id = ? AND idempotency_key = ?
                  AND {self._scope_sql()}
                """,
                (
                    scope["user_id"],
                    state.client_id,
                    request.idempotency_key,
                    *self._row_scope_values(scope),
                ),
            ).fetchone()
        if row is None:
            return None
        result = json.loads(row["response_json"])
        result["idempotent_replay"] = True
        return result

    def _persist_delivery(
        self,
        *,
        request: CAGRequest,
        scope: Mapping[str, str | None],
        response: Mapping[str, Any],
        explanation: Mapping[str, Any],
        cache_key: str,
    ) -> None:
        state = request.client_state
        token_usage = response["token_usage"]
        cache = response["cache"]
        with self._connect(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO cag_deliveries (
                    id, context_pack_id, user_id, workspace_id, project_id,
                    repository_id, task_id, session_id, agent_id, client_id,
                    client_type, delivery_mode, known_state_json,
                    authoritative_state_json, response_json, explanation_json,
                    full_tokens, delivered_tokens, avoided_tokens, savings_ratio,
                    server_cache_hit, pack_reused, fallback_reason,
                    estimator_version, policy_version, cache_key,
                    idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    response["delivery_id"],
                    response["authoritative_state"]["context_pack_id"],
                    scope["user_id"],
                    scope["workspace_id"],
                    scope["project_id"],
                    scope["repository_id"],
                    scope["task_id"],
                    scope["session_id"],
                    scope["agent_id"],
                    state.client_id if state else None,
                    state.client_type if state else None,
                    response["delivery_mode"],
                    _stable_json(state.as_dict() if state else {}),
                    _stable_json(response["authoritative_state"]),
                    _stable_json(response),
                    _stable_json(explanation),
                    token_usage["full_context_estimate"],
                    token_usage["delivered_estimate"],
                    token_usage["avoided_estimate"],
                    token_usage["savings_ratio"],
                    int(bool(cache["server_hit"])),
                    int(bool(cache["pack_reused"])),
                    cache.get("fallback_reason"),
                    self.estimator.version,
                    self.policy_version,
                    cache_key,
                    request.idempotency_key,
                    time.time(),
                ),
            )

    def deliver(self, request: CAGRequest) -> dict[str, Any]:
        started = time.perf_counter()
        request = request.validated(self.settings)
        scope, task, session = self._resolve_scope(request.context)
        replay = self._idempotent_replay(request, scope)
        if replay is not None:
            return replay
        state = request.client_state
        objective = request.context.objective or str(task.get("objective") or "")
        token_limit = (
            request.context.max_tokens or self.settings.CONTEXT_DEFAULT_MAX_TOKENS
        )
        skills = self._skills(scope, objective=objective, token_limit=token_limit)
        handoffs = self._handoffs(scope)
        cache_key = self.cache_key(
            scope=scope,
            request=request.context,
            task=task,
            session=session,
            skills=skills,
            handoffs=handoffs,
            capabilities=state.capabilities if state else (),
        )
        pack, _entry, pack_reused, hot_hit, cache_fallback = self._current_pack(
            cache_key=cache_key,
            scope=scope,
            request=request.context,
            task=task,
            session=session,
            skills=skills,
            handoffs=handoffs,
        )
        # The pack is authoritative for which skills survived context budgeting.
        selected_skill_ids = {str(item["id"]) for item in pack.get("skills", [])}
        skills = [item for item in skills if str(item["id"]) in selected_skill_ids]
        authoritative = self._authoritative_state(
            pack=pack, session=session, skills=skills, handoffs=handoffs
        )
        skill_state = self._classify_skills(current=skills, state=state, scope=scope)
        handoff_state = self._classify_handoffs(
            current=handoffs, state=state, scope=scope
        )
        client_pack: dict[str, Any] | None = None
        invalid_reason: str | None = None
        if state is not None:
            if state.known_checkpoint_version is not None:
                current_checkpoint = int(str(authoritative["checkpoint_version"]))
                if state.known_checkpoint_version > current_checkpoint:
                    invalid_reason = "invalid_checkpoint_version"
            client_pack, pack_reason = self._client_pack(state, scope, request.context)
            invalid_reason = invalid_reason or pack_reason

        warnings: list[dict[str, object]] = []
        context_payload: dict[str, object] = {
            "full": None,
            "delta": None,
            "mandatory_refresh": [],
        }
        delta_started = time.perf_counter()
        context_delta: dict[str, Any] | None = None
        mode = "full"
        fallback_reason: str | None = None
        cache_requires_fallback = bool(
            cache_fallback and cache_fallback.startswith("cache_lookup_failed:")
        )
        if not self.settings.CAG_ENABLED:
            warnings.append(
                {"code": "cag_disabled", "message": "CAG optimization is disabled."}
            )
            context_payload["full"] = pack
        elif state is None:
            context_payload["full"] = pack
        elif "context_delta" not in set(state.capabilities):
            context_payload["full"] = pack
            warnings.append(
                {
                    "code": "delta_not_supported",
                    "message": "Client lacks context_delta capability; full context returned.",
                }
            )
        elif cache_requires_fallback:
            mode = "fallback_full"
            fallback_reason = cache_fallback
            context_payload["full"] = pack
            warnings.append(
                {
                    "code": "cache_fallback",
                    "message": "Cached state was unusable; canonical context was rebuilt.",
                    "reason": cache_fallback,
                }
            )
        elif invalid_reason:
            mode = "fallback_full"
            fallback_reason = invalid_reason
            context_payload["full"] = pack
            warnings.append(
                {
                    "code": "client_state_rejected",
                    "message": "Client state could not be verified; canonical context returned.",
                    "reason": invalid_reason,
                }
            )
        elif client_pack is None:
            context_payload["full"] = pack
        else:
            try:
                context_delta = self.context_delta(
                    client_pack,
                    pack,
                    known_checkpoint_version=state.known_checkpoint_version,
                    agent_id=scope.get("agent_id"),
                )
                context_payload["delta"] = context_delta
                has_skill_changes = any(
                    skill_state[key]
                    for key in (
                        "updated",
                        "missing",
                        "stale_client_versions",
                        "removed",
                    )
                )
                has_handoff_changes = any(
                    handoff_state[key]
                    for key in ("updated", "missing", "refresh_required")
                )
                refresh = bool(
                    skill_state["stale_client_versions"]
                    or skill_state["removed"]
                    or handoff_state["refresh_required"]
                )
                if refresh:
                    mode = "refresh_required"
                    context_payload["mandatory_refresh"] = [
                        *skill_state["stale_client_versions"],
                        *skill_state["removed"],
                        *handoff_state["refresh_required"],
                    ]
                elif (
                    not context_delta["changed"]
                    and not has_skill_changes
                    and not has_handoff_changes
                ):
                    mode = "no_change"
                    context_payload["delta"] = {
                        "from_context_pack_id": client_pack["context_pack_id"],
                        "to_context_pack_id": pack["context_pack_id"],
                        "status": "current",
                    }
                else:
                    mode = "delta"
            except Exception as exc:
                mode = "fallback_full"
                fallback_reason = f"delta_generation_failed:{type(exc).__name__}"
                context_payload = {"full": pack, "delta": None, "mandatory_refresh": []}
                warnings.append(
                    {
                        "code": "delta_fallback",
                        "message": "Delta generation failed; canonical context returned.",
                    }
                )
        with self._metrics_lock:
            self._metrics.delta_generation_latency_seconds_total += (
                time.perf_counter() - delta_started
            )

        full_tokens = max(
            1,
            int(pack.get("token_usage", {}).get("estimated_total", 0)),
            self.estimator.estimate(pack),
        )
        delivery_material = {
            "context": context_payload,
            "skills": skill_state,
            "handoffs": handoff_state,
            "warnings": warnings,
        }
        delivered_tokens = (
            full_tokens
            if mode in {"full", "fallback_full"}
            else self.estimator.estimate(delivery_material)
        )
        if mode == "delta" and delivered_tokens > (
            full_tokens * self.settings.CONTEXT_DELTA_MAX_RATIO
        ):
            mode = "full"
            context_payload = {"full": pack, "delta": None, "mandatory_refresh": []}
            delivered_tokens = full_tokens
            warnings.append(
                {
                    "code": "delta_larger_than_threshold",
                    "message": "Full context is smaller than the safe delta.",
                }
            )
        if mode == "refresh_required" and delivered_tokens > full_tokens:
            context_payload["full"] = pack
            delivered_tokens = full_tokens
        delivered_tokens = max(0, delivered_tokens)
        avoided_tokens = max(0, full_tokens - delivered_tokens)
        savings_ratio = avoided_tokens / full_tokens if full_tokens else 0.0
        fallback_reason = fallback_reason or (
            cache_fallback if mode == "fallback_full" else None
        )
        response: dict[str, Any] = {
            "delivery_id": _identifier("delivery"),
            "delivery_mode": mode,
            "schema_version": CAG_SCHEMA_VERSION,
            "policy_version": self.policy_version,
            "authoritative_state": authoritative,
            "context": context_payload,
            "skills": skill_state,
            "handoffs": handoff_state,
            "warnings": warnings,
            "token_usage": {
                "full_context_estimate": full_tokens,
                "delivered_estimate": delivered_tokens,
                "avoided_estimate": avoided_tokens,
                "savings_ratio": round(savings_ratio, 6),
                "unchanged_skill_tokens_avoided": sum(
                    int(item.get("token_estimate", 0))
                    for item in skills
                    if any(
                        unchanged["id"] == item["id"]
                        for unchanged in skill_state["unchanged"]
                    )
                ),
                "context_delta_tokens": self.estimator.estimate(context_delta)
                if context_delta
                else 0,
                "skill_delta_tokens": self.estimator.estimate(skill_state["updated"]),
                "reference_tokens": self.estimator.estimate(pack.get("references", [])),
                "fallback_tokens": full_tokens if mode == "fallback_full" else 0,
                "estimator_version": self.estimator.version,
            },
            "cache": {
                "server_hit": pack_reused,
                "hot_cache_hit": hot_hit,
                "pack_reused": pack_reused,
                "skill_hits": len(skill_state["unchanged"]),
                "skill_misses": len(skill_state["updated"])
                + len(skill_state["missing"]),
                "fallback": mode == "fallback_full",
                "fallback_reason": fallback_reason,
                "cache_key": cache_key,
            },
        }
        explanation = {
            "delivery_mode_reason": {
                "full": "client supplied no reusable state or lacks delta capability",
                "delta": "verified client state changed and delta is smaller than full",
                "no_change": "all authoritative versions and dependencies are current",
                "refresh_required": "client holds stale, removed, or invalid artifacts",
                "fallback_full": "optimized state was unverifiable or delta generation failed",
            }[mode],
            "accepted_client_state": state.as_dict()
            if state and not invalid_reason
            else {},
            "rejected_client_state_reason": invalid_reason,
            "scope_filters": dict(scope),
            "policy_versions": {
                "cag": self.policy_version,
                "context": self.context_service.policy_version,
                "token_estimator": self.estimator.version,
            },
            "cache_key": cache_key,
            "pack_reused": pack_reused,
            "skill_classification": {
                key: len(value) for key, value in skill_state.items()
            },
            "handoff_classification": {
                key: len(value) for key, value in handoff_state.items()
            },
            "token_formula": "avoided=max(0,full-delivered); ratio=avoided/full",
            "fallback_reason": fallback_reason,
        }
        try:
            self._persist_delivery(
                request=request,
                scope=scope,
                response=response,
                explanation=explanation,
                cache_key=cache_key,
            )
        except sqlite3.IntegrityError:
            replay = self._idempotent_replay(request, scope)
            if replay is not None:
                return replay
            raise
        except Exception as exc:
            response["warnings"].append(
                {
                    "code": "delivery_telemetry_unavailable",
                    "message": "Safe delivery succeeded but telemetry persistence failed.",
                }
            )
            with self._metrics_lock:
                self._metrics.telemetry_errors_total += 1
                self._metrics.last_error = f"{type(exc).__name__}: {exc}"
        latency = time.perf_counter() - started
        with self._metrics_lock:
            self._metrics.deliveries_total += 1
            metric_field = {
                "full": "full_total",
                "delta": "delta_total",
                "no_change": "no_change_total",
                "refresh_required": "refresh_required_total",
                "fallback_full": "fallback_total",
            }[mode]
            setattr(
                self._metrics,
                metric_field,
                int(getattr(self._metrics, metric_field)) + 1,
            )
            self._metrics.delivery_latency_seconds_total += latency
            self._metrics.estimated_tokens_full_total += full_tokens
            self._metrics.estimated_tokens_delivered_total += delivered_tokens
            self._metrics.estimated_tokens_avoided_total += avoided_tokens
            self._metrics.skill_delta_total += len(skill_state["updated"])
            self._metrics.context_delta_total += int(context_delta is not None)
            self._metrics.handoff_delta_total += len(handoff_state["updated"])
        return response

    @staticmethod
    def _assert_delivery_scope(
        row: sqlite3.Row, scope: Mapping[str, str | None]
    ) -> None:
        for scope_field in (
            "user_id",
            "workspace_id",
            "project_id",
            "repository_id",
            "task_id",
            "agent_id",
        ):
            if scope.get(scope_field) is None or row[scope_field] != scope[scope_field]:
                raise ContinuityError(
                    "delivery was not found in the requested scope", code="not_found"
                )
        if (
            scope.get("session_id") is not None
            and row["session_id"] != scope["session_id"]
        ):
            raise ContinuityError(
                "delivery was not found in the requested scope", code="not_found"
            )

    def get_delivery(
        self, delivery_id: str, scope: Mapping[str, str | None]
    ) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM cag_deliveries WHERE id = ?", (delivery_id,)
            ).fetchone()
        if row is None:
            raise ContinuityError("delivery was not found", code="not_found")
        self._assert_delivery_scope(row, scope)
        return json.loads(row["response_json"])

    def explain(
        self, delivery_id: str, scope: Mapping[str, str | None]
    ) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM cag_deliveries WHERE id = ?", (delivery_id,)
            ).fetchone()
        if row is None:
            raise ContinuityError("delivery was not found", code="not_found")
        self._assert_delivery_scope(row, scope)
        return {
            "delivery_id": delivery_id,
            **json.loads(row["explanation_json"]),
            "authoritative_state": json.loads(row["authoritative_state_json"]),
        }

    def record_feedback(
        self,
        delivery_id: str,
        *,
        scope: Mapping[str, str | None],
        agent_id: str,
        used_skill_versions: Sequence[str] = (),
        used_context_sections: Sequence[str] = (),
        expanded_references: Sequence[str] = (),
        outcome: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        response = self.get_delivery(delivery_id, scope)
        if scope.get("agent_id") != agent_id:
            raise ContinuityError(
                "agent is not authorized for delivery feedback", code="not_found"
            )
        authoritative_skills = {
            f"{skill_id}@{version}"
            for skill_id, version in response["authoritative_state"]["skills"].items()
        }
        unknown = set(used_skill_versions) - authoritative_skills
        if unknown:
            raise ContinuityError(
                "feedback references an unavailable skill version",
                code="scope_mismatch",
            )
        with self._connect(immediate=True) as conn:
            if idempotency_key:
                prior = conn.execute(
                    """
                    SELECT * FROM cag_delivery_feedback
                    WHERE delivery_id = ? AND idempotency_key = ?
                    """,
                    (delivery_id, idempotency_key),
                ).fetchone()
                if prior:
                    return {"id": prior["id"], "idempotent_replay": True}
            feedback_id = _identifier("delivery_feedback")
            conn.execute(
                """
                INSERT INTO cag_delivery_feedback (
                    id, delivery_id, agent_id, used_skill_versions_json,
                    used_context_sections_json, expanded_references_json,
                    outcome, idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    feedback_id,
                    delivery_id,
                    agent_id,
                    _stable_json(list(used_skill_versions)),
                    _stable_json(list(used_context_sections)),
                    _stable_json(list(expanded_references)),
                    outcome,
                    idempotency_key,
                    time.time(),
                ),
            )
        skill_scope = self._skill_scope(scope)
        for exact in used_skill_versions:
            skill_id, version_text = exact.rsplit("@", 1)
            self.skill_service.record_usage(
                skill_id,
                int(version_text),
                scope=skill_scope,
                usage_type="used",
                agent_id=agent_id,
                context_pack_id=response["authoritative_state"]["context_pack_id"],
                task_id=scope.get("task_id"),
                session_id=scope.get("session_id"),
                metadata={"delivery_id": delivery_id},
                idempotency_key=f"delivery:{delivery_id}:used:{exact}",
            )
        return {"id": feedback_id, "idempotent_replay": False}

    def inspect_cache(
        self,
        scope: Mapping[str, str | None],
        *,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        query = f"SELECT * FROM cag_cache_entries WHERE {self._scope_sql()}"
        values: list[object] = list(self._row_scope_values(scope))
        if status:
            query += " AND status = ?"
            values.append(status)
        query += " ORDER BY created_at DESC, id"
        with self._connect() as conn:
            rows = conn.execute(query, values).fetchall()
        return [
            {key: row[key] for key in row.keys() if key not in {"metadata_json"}}
            for row in rows
        ]

    def inspect_cache_entry(
        self, cache_entry_id: str, scope: Mapping[str, str | None]
    ) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM cag_cache_entries WHERE id = ? AND {self._scope_sql()}",
                (cache_entry_id, *self._row_scope_values(scope)),
            ).fetchone()
            if row is None:
                raise ContinuityError("cache entry was not found", code="not_found")
            dependencies = [
                dict(item)
                for item in conn.execute(
                    """
                    SELECT dependency_type, dependency_id, dependency_version,
                           dependency_hash, created_at
                    FROM cag_cache_dependencies WHERE cache_entry_id = ?
                    ORDER BY dependency_type, dependency_id
                    """,
                    (cache_entry_id,),
                ).fetchall()
            ]
        result = dict(row)
        result["metadata"] = json.loads(result.pop("metadata_json"))
        result["dependencies"] = dependencies
        return result

    def invalidate(
        self,
        *,
        scope: Mapping[str, str | None],
        cache_entry_id: str | None = None,
        reason: str = "manual_invalidation",
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            if cache_entry_id:
                rows = conn.execute(
                    f"SELECT * FROM cag_cache_entries WHERE id = ? AND {self._scope_sql()}",
                    (cache_entry_id, *self._row_scope_values(scope)),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT * FROM cag_cache_entries WHERE {self._scope_sql()}",
                    self._row_scope_values(scope),
                ).fetchall()
        if cache_entry_id and not rows:
            raise ContinuityError("cache entry was not found", code="not_found")
        for row in rows:
            self._mark_entry(row, status="invalid", reason=reason, actor_id=actor_id)
        return {"invalidated": len(rows), "reason": reason}

    def invalidate_scope(
        self,
        *,
        user_id: str,
        workspace_id: str,
        project_id: str,
        repository_id: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        agent_id: str | None = None,
        cache_entry_id: str | None = None,
        reason: str = "manual_invalidation",
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        """Invalidate a verified project subtree without exposing other scopes."""
        if not user_id or not workspace_id or not project_id:
            raise ContinuityError(
                "user, workspace, and project are required for cache invalidation",
                code="invalid_scope",
            )
        filters: list[tuple[str, str]] = [
            ("user_id", user_id),
            ("workspace_id", workspace_id),
            ("project_id", project_id),
        ]
        for column, value in (
            ("repository_id", repository_id),
            ("task_id", task_id),
            ("session_id", session_id),
            ("agent_id", agent_id),
        ):
            if value is not None:
                filters.append((column, value))
        where = " AND ".join(f"{column} = ?" for column, _value in filters)
        values: list[object] = [value for _column, value in filters]
        if cache_entry_id:
            where = f"id = ? AND {where}"
            values.insert(0, cache_entry_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM cag_cache_entries WHERE {where}", values
            ).fetchall()
        if cache_entry_id and not rows:
            raise ContinuityError("cache entry was not found", code="not_found")
        for row in rows:
            self._mark_entry(row, status="invalid", reason=reason, actor_id=actor_id)
        return {"invalidated": len(rows), "reason": reason}

    def prune_expired(self) -> dict[str, int]:
        now = time.time()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM cag_cache_entries
                WHERE status = 'fresh' AND expires_at IS NOT NULL AND expires_at <= ?
                """,
                (now,),
            ).fetchall()
        for row in rows:
            self._mark_entry(row, status="expired", reason="ttl_expired")
        return {"expired": len(rows)}

    def stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            cache_counts = {
                str(row["status"]): int(row["count"])
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS count FROM cag_cache_entries GROUP BY status"
                ).fetchall()
            }
            delivery_counts = {
                str(row["delivery_mode"]): int(row["count"])
                for row in conn.execute(
                    "SELECT delivery_mode, COUNT(*) AS count FROM cag_deliveries GROUP BY delivery_mode"
                ).fetchall()
            }
            durable_entries = int(
                conn.execute("SELECT COUNT(*) FROM cag_cache_entries").fetchone()[0]
            )
            feedback_pending = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM cag_deliveries d
                    WHERE NOT EXISTS (
                        SELECT 1 FROM cag_delivery_feedback f WHERE f.delivery_id = d.id
                    )
                    """
                ).fetchone()[0]
            )
        with self._metrics_lock:
            metrics = asdict(self._metrics)
        return {
            **metrics,
            "enabled": self.settings.CAG_ENABLED,
            "policy_version": self.policy_version,
            "schema_version": CAG_SCHEMA_VERSION,
            "migration_available": CURRENT_SCHEMA_VERSION >= 11,
            "cache_counts": cache_counts,
            "delivery_counts": delivery_counts,
            "durable_entries": durable_entries,
            "feedback_pending": feedback_pending,
            "hot_cache": self.hot_cache.stats(),
            "lazy_rebuild": self.settings.CACHE_LAZY_REBUILD,
            "token_estimator_version": self.estimator.version,
        }

    def health(self) -> dict[str, Any]:
        stats = self.stats()
        return {
            "available": True,
            "enabled": stats["enabled"],
            "migration_available": stats["migration_available"],
            "policy_version": stats["policy_version"],
            "hot_cache_capacity": stats["hot_cache"]["max_entries"],
            "cache_counts": stats["cache_counts"],
            "fallback_count": stats["fallback_total"],
            "latest_invalidation_error": stats["last_invalidation_error"],
        }
