"""Retrieval telemetry, explainable utility, and reversible retention lifecycle."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from recall_mcp.continuity import ContinuityError
from recall_mcp.runtime import Settings
from recall_mcp.store.migrations import run_migrations


UTILITY_POLICY_VERSION = "utility-v1"
UTILITY_MIN = 0.0
UTILITY_MAX = 1.0

DEFAULT_UTILITY_WEIGHTS: dict[str, float] = {
    "initial_importance": 1.0,
    "successful_reuse": 0.12,
    "cross_session_reuse": 0.06,
    "cross_agent_reuse": 0.05,
    "task_success": 0.18,
    "trust": 0.04,
    "freshness": 0.08,
    "pinned": 0.5,
    "staleness_penalty": 0.12,
    "duplicate_penalty": 0.08,
    "ignored_penalty": 0.04,
    "correction_penalty": 0.3,
    "contradiction_penalty": 0.25,
}

USAGE_TYPES = {
    "returned",
    "selected",
    "injected",
    "expanded",
    "used",
    "ignored",
    "corrected",
    "rejected",
}


@dataclass(frozen=True)
class UtilityComponents:
    initial_importance: float
    successful_reuse: float
    cross_session_reuse: float
    cross_agent_reuse: float
    task_success: float
    trust: float
    freshness: float
    pinned: float
    staleness_penalty: float
    duplicate_penalty: float
    ignored_penalty: float
    correction_penalty: float
    contradiction_penalty: float

    def score(self) -> float:
        positive = (
            self.initial_importance
            + self.successful_reuse
            + self.cross_session_reuse
            + self.cross_agent_reuse
            + self.task_success
            + self.trust
            + self.freshness
            + self.pinned
        )
        negative = (
            self.staleness_penalty
            + self.duplicate_penalty
            + self.ignored_penalty
            + self.correction_penalty
            + self.contradiction_penalty
        )
        return min(UTILITY_MAX, max(UTILITY_MIN, positive - negative))

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class RetrievalFeedbackService:
    """Canonical application service for retrieval feedback and utility."""

    def __init__(self, db_path: Path, settings: Settings):
        self.db_path = Path(db_path)
        self.settings = settings
        run_migrations(self.db_path)
        unknown = set(settings.UTILITY_WEIGHTS) - set(DEFAULT_UTILITY_WEIGHTS)
        if unknown:
            raise ValueError("unknown utility weights: " + ", ".join(sorted(unknown)))
        self.weights = {**DEFAULT_UTILITY_WEIGHTS, **settings.UTILITY_WEIGHTS}
        self.policy_version = settings.UTILITY_POLICY_VERSION or UTILITY_POLICY_VERSION
        self._health_lock = threading.Lock()
        self._last_error = ""
        self._error_count = 0

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

    def record_error(self, error: BaseException) -> None:
        with self._health_lock:
            self._error_count += 1
            self._last_error = f"{type(error).__name__}: telemetry operation failed"

    def health(self) -> dict[str, object]:
        with self._health_lock:
            return {
                "policy_version": self.policy_version,
                "shadow_mode": self.settings.UTILITY_SHADOW_MODE,
                "ranking_weight": self.settings.UTILITY_RANKING_WEIGHT,
                "error_count": self._error_count,
                "last_error": self._last_error,
            }

    def telemetry_stats(self) -> dict[str, object]:
        """Return content-free operational counts for diagnostics and metrics."""
        with self._connect() as conn:
            result_flags = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(selected), 0) AS selected,
                       COALESCE(SUM(injected), 0) AS injected,
                       COALESCE(SUM(used), 0) AS used
                FROM retrieval_results
                """
            ).fetchone()
            usage = {
                str(row["usage_type"]): int(row["count"])
                for row in conn.execute(
                    """
                    SELECT usage_type, COUNT(*) AS count
                    FROM context_usage GROUP BY usage_type
                    """
                ).fetchall()
            }
            lifecycle = {
                str(row["lifecycle_state"]): int(row["count"])
                for row in conn.execute(
                    """
                    SELECT lifecycle_state, COUNT(*) AS count
                    FROM memories GROUP BY lifecycle_state
                    """
                ).fetchall()
            }
            counts = {
                table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "retrieval_runs",
                    "context_packs",
                    "task_outcomes",
                    "utility_history",
                    "memory_lifecycle_audit",
                )
            }
        return {
            **self.health(),
            **counts,
            "retrieval_results": int(result_flags["total"]),
            "selected_results": int(result_flags["selected"]),
            "injected_results": int(result_flags["injected"]),
            "used_results": int(result_flags["used"]),
            "usage": usage,
            "lifecycle": lifecycle,
        }

    @staticmethod
    def _memory(
        conn: sqlite3.Connection,
        memory_id: str,
        *,
        scope: Mapping[str, str] | None = None,
    ) -> sqlite3.Row:
        sql = "SELECT * FROM memories WHERE id = ?"
        params: list[object] = [memory_id]
        for column, value in (scope or {}).items():
            if column not in {
                "user_id",
                "workspace_id",
                "project_id",
                "repository_id",
                "task_id",
            }:
                continue
            sql += f" AND {column} = ?"
            params.append(value)
        row = conn.execute(sql, params).fetchone()
        if row is None:
            raise ContinuityError(
                f"memory {memory_id} was not found in the requested scope",
                code="not_found",
            )
        return row

    @staticmethod
    def _ensure_agent(
        conn: sqlite3.Connection, agent_id: str | None, *, now: float
    ) -> None:
        if not agent_id:
            return
        if conn.execute("SELECT 1 FROM agents WHERE id = ?", (agent_id,)).fetchone():
            conn.execute(
                "UPDATE agents SET last_seen_at = ? WHERE id = ?", (now, agent_id)
            )
            return
        conn.execute(
            """
            INSERT INTO agents (
                id, name, system_type, trust_level, write_policy,
                created_at, last_seen_at
            ) VALUES (?, ?, 'other', 'standard', 'standard', ?, ?)
            """,
            (agent_id, agent_id, now, now),
        )

    @staticmethod
    def _metadata_rows(
        rows: Sequence[sqlite3.Row], usage_type: str | None = None
    ) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        for row in rows:
            if usage_type is not None and row["usage_type"] != usage_type:
                continue
            try:
                decoded = json.loads(row["metadata_json"] or "{}")
            except json.JSONDecodeError:
                decoded = {}
            values.append(decoded if isinstance(decoded, dict) else {})
        return values

    def _calculate(
        self,
        conn: sqlite3.Connection,
        memory_id: str,
        *,
        now: float,
    ) -> tuple[float, UtilityComponents]:
        memory = self._memory(conn, memory_id)
        usage = conn.execute(
            """
            SELECT u.*, p.task_id, p.session_id
            FROM context_usage u
            JOIN context_packs p ON p.id = u.context_pack_id
            WHERE u.memory_id = ?
            """,
            (memory_id,),
        ).fetchall()
        counts: dict[str, int] = {kind: 0 for kind in USAGE_TYPES}
        for row in usage:
            counts[str(row["usage_type"])] += 1
        useful = [row for row in usage if row["usage_type"] in {"expanded", "used"}]
        sessions = {row["session_id"] for row in useful if row["session_id"]}
        agents = {row["agent_id"] for row in useful if row["agent_id"]}

        successful_rows = conn.execute(
            """
            SELECT DISTINCT o.id
            FROM task_outcomes o
            JOIN context_packs p
              ON p.task_id = o.task_id
             AND (o.session_id IS NULL OR p.session_id = o.session_id)
            JOIN context_usage u
              ON u.context_pack_id = p.id AND u.memory_id = ?
            WHERE o.successful = 1 AND u.usage_type = 'used'
            """,
            (memory_id,),
        ).fetchall()
        outcome_penalties = conn.execute(
            """
            SELECT COALESCE(SUM(o.user_correction_count), 0) AS corrections,
                   COALESCE(SUM(o.rework_required), 0) AS rework
            FROM task_outcomes o
            JOIN context_packs p
              ON p.task_id = o.task_id
             AND (o.session_id IS NULL OR p.session_id = o.session_id)
            JOIN context_usage u
              ON u.context_pack_id = p.id AND u.memory_id = ?
            WHERE u.usage_type IN ('used', 'expanded')
            """,
            (memory_id,),
        ).fetchone()
        correction_count = counts["corrected"] + int(outcome_penalties["corrections"])
        rework_count = int(outcome_penalties["rework"])

        metadata = self._metadata_rows(usage)
        duplicate_count = sum(bool(item.get("duplicate")) for item in metadata)
        contradiction_count = sum(bool(item.get("contradiction")) for item in metadata)
        trust_raw = 0.5
        if memory["agent"]:
            agent = conn.execute(
                "SELECT trust_level FROM agents WHERE id = ?", (memory["agent"],)
            ).fetchone()
            if agent:
                trust_raw = {
                    "low": 0.0,
                    "standard": 0.5,
                    "trusted": 1.0,
                }.get(str(agent["trust_level"]), 0.5)

        age_days = max(0, int((now - float(memory["created_at"])) // 86400))
        freshness_raw = max(0.0, 1.0 - (age_days / 365.0))
        last_use = memory["last_used_at"] or memory["created_at"]
        stale_raw = 1.0 if now - float(last_use) >= 180 * 86400 else 0.0
        admission = {"cold": 0.08, "active": 0.22, "persist": 0.32}.get(
            str(memory["tier"]), 0.15
        )

        w = self.weights
        components = UtilityComponents(
            initial_importance=admission * w["initial_importance"],
            successful_reuse=min(counts["used"] + counts["expanded"], 3)
            * w["successful_reuse"],
            cross_session_reuse=max(0, min(len(sessions) - 1, 3))
            * w["cross_session_reuse"],
            cross_agent_reuse=max(0, min(len(agents) - 1, 3)) * w["cross_agent_reuse"],
            task_success=min(len(successful_rows), 3) * w["task_success"],
            trust=trust_raw * w["trust"],
            freshness=freshness_raw * w["freshness"],
            pinned=(1.0 if memory["pinned"] else 0.0) * w["pinned"],
            staleness_penalty=stale_raw * w["staleness_penalty"],
            duplicate_penalty=min(duplicate_count, 3) * w["duplicate_penalty"],
            ignored_penalty=max(0, counts["ignored"] - counts["used"])
            * w["ignored_penalty"],
            correction_penalty=correction_count * w["correction_penalty"],
            contradiction_penalty=(contradiction_count + rework_count)
            * w["contradiction_penalty"],
        )
        return components.score(), components

    def calculate_utility(
        self, memory_id: str, *, now: float | None = None
    ) -> tuple[float, UtilityComponents]:
        calculated_at = time.time() if now is None else now
        with self._connect() as conn:
            return self._calculate(conn, memory_id, now=calculated_at)

    def _persist_utility(
        self,
        conn: sqlite3.Connection,
        memory_id: str,
        *,
        reason: str,
        now: float,
    ) -> tuple[float, UtilityComponents]:
        memory = self._memory(conn, memory_id)
        previous = float(memory["utility_score"] or 0.0)
        score, components = self._calculate(conn, memory_id, now=now)
        conn.execute(
            """
            UPDATE memories
            SET utility_score = ?, utility_policy_version = ?
            WHERE id = ?
            """,
            (score, self.policy_version, memory_id),
        )
        conn.execute(
            """
            INSERT INTO utility_history (
                id, memory_id, previous_score, new_score, reason,
                components_json, policy_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _id("utility"),
                memory_id,
                previous,
                score,
                reason,
                _json(components.as_dict()),
                self.policy_version,
                now,
            ),
        )
        return score, components

    def recalculate_utility(
        self, memory_id: str, *, reason: str, now: float | None = None
    ) -> dict[str, object]:
        calculated_at = time.time() if now is None else now
        with self._connect(immediate=True) as conn:
            score, components = self._persist_utility(
                conn, memory_id, reason=reason, now=calculated_at
            )
        return {
            "memory_id": memory_id,
            "score": score,
            "components": components.as_dict(),
            "policy_version": self.policy_version,
            "reason": reason,
        }

    def explain_utility(
        self,
        memory_id: str,
        *,
        scope: Mapping[str, str] | None = None,
        now: float | None = None,
    ) -> dict[str, object]:
        calculated_at = time.time() if now is None else now
        with self._connect() as conn:
            memory = self._memory(conn, memory_id, scope=scope)
            score, components = self._calculate(conn, memory_id, now=calculated_at)
            return {
                "memory_id": memory_id,
                "stored_score": float(memory["utility_score"] or 0.0),
                "calculated_score": score,
                "components": components.as_dict(),
                "weights": dict(self.weights),
                "policy_version": self.policy_version,
                "lifecycle_state": memory["lifecycle_state"],
                "pinned": bool(memory["pinned"]),
                "expires_at": memory["expires_at"],
                "retention_review_at": memory["retention_review_at"],
            }

    def utility_history(
        self,
        memory_id: str,
        *,
        scope: Mapping[str, str] | None = None,
    ) -> list[dict[str, object]]:
        with self._connect() as conn:
            self._memory(conn, memory_id, scope=scope)
            rows = conn.execute(
                """
                SELECT * FROM utility_history
                WHERE memory_id = ? ORDER BY created_at, id
                """,
                (memory_id,),
            ).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            item["components"] = json.loads(item.pop("components_json"))
            result.append(item)
        return result

    def current_utility_scores(self, memory_ids: Sequence[str]) -> dict[str, float]:
        if not memory_ids:
            return {}
        with self._connect() as conn:
            placeholders = ",".join("?" for _ in memory_ids)
            rows = conn.execute(
                f"""
                SELECT id, utility_score FROM memories
                WHERE id IN ({placeholders})
                """,
                list(memory_ids),
            ).fetchall()
        return {str(row["id"]): float(row["utility_score"] or 0.0) for row in rows}

    def record_retrieval_run(
        self,
        *,
        query: str,
        mode: str,
        requested_limit: int,
        latency_ms: float,
        results: list[dict[str, Any]],
        user_id: str | None = None,
        workspace_id: str | None = None,
        project_id: str | None = None,
        repository_id: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        agent_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        now = time.time()
        with self._connect(immediate=True) as conn:
            self._ensure_agent(conn, agent_id, now=now)
            scope = {
                key: value
                for key, value in {
                    "user_id": user_id,
                    "workspace_id": workspace_id,
                    "project_id": project_id,
                    "repository_id": repository_id,
                    "task_id": task_id,
                }.items()
                if value is not None
            }
            if task_id:
                task = conn.execute(
                    """
                    SELECT user_id, workspace_id, project_id, repository_id
                    FROM tasks WHERE id = ?
                    """,
                    (task_id,),
                ).fetchone()
                if task is None:
                    raise ContinuityError(
                        f"task {task_id} was not found", code="not_found"
                    )
                for field, requested in (
                    ("user_id", user_id),
                    ("workspace_id", workspace_id),
                    ("project_id", project_id),
                    ("repository_id", repository_id),
                ):
                    if requested is not None and task[field] != requested:
                        raise ContinuityError(
                            "retrieval scope does not match the task",
                            code="scope_mismatch",
                        )
            if session_id:
                session = conn.execute(
                    "SELECT task_id FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if session is None:
                    raise ContinuityError(
                        f"session {session_id} was not found", code="not_found"
                    )
                if task_id is None or session["task_id"] != task_id:
                    raise ContinuityError(
                        "retrieval session is outside the requested task",
                        code="scope_mismatch",
                    )
            if idempotency_key:
                prior = conn.execute(
                    """
                    SELECT * FROM retrieval_runs
                    WHERE agent_id IS ? AND idempotency_key = ?
                    """,
                    (agent_id, idempotency_key),
                ).fetchone()
                if prior:
                    canonical = (
                        user_id,
                        workspace_id,
                        project_id,
                        repository_id,
                        task_id,
                        session_id,
                        query,
                        mode,
                        requested_limit,
                    )
                    existing = tuple(
                        prior[field]
                        for field in (
                            "user_id",
                            "workspace_id",
                            "project_id",
                            "repository_id",
                            "task_id",
                            "session_id",
                            "query",
                            "mode",
                            "requested_limit",
                        )
                    )
                    prior_results = conn.execute(
                        """
                        SELECT memory_id, rank, shadow_rank, utility_score,
                               shadow_score
                        FROM retrieval_results
                        WHERE retrieval_run_id = ? ORDER BY rank
                        """,
                        (prior["id"],),
                    ).fetchall()
                    if existing != canonical or [
                        str(row["memory_id"]) for row in prior_results
                    ] != [str(result["id"]) for result in results]:
                        raise ContinuityError(
                            "idempotency key was reused with a different retrieval",
                            code="idempotency_conflict",
                        )
                    for result, prior_result in zip(results, prior_results):
                        result.update(
                            {
                                "retrieval_run_id": prior["id"],
                                "production_rank": prior_result["rank"],
                                "shadow_rank": prior_result["shadow_rank"],
                                "utility_score": prior_result["utility_score"],
                                "shadow_score": prior_result["shadow_score"],
                                "utility_policy_version": self.policy_version,
                            }
                        )
                    return str(prior["id"])
            run_id = _id("retrieval")
            conn.execute(
                """
                INSERT INTO retrieval_runs (
                    id, user_id, workspace_id, project_id, repository_id,
                    task_id, session_id, agent_id, query, mode, requested_limit,
                    created_at, latency_ms, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    user_id,
                    workspace_id,
                    project_id,
                    repository_id,
                    task_id,
                    session_id,
                    agent_id,
                    query,
                    mode,
                    requested_limit,
                    now,
                    max(0.0, latency_ms),
                    idempotency_key,
                ),
            )
            scored: list[tuple[int, dict[str, Any], float, float]] = []
            for rank, result in enumerate(results, start=1):
                memory = self._memory(conn, str(result["id"]), scope=scope)
                utility = float(memory["utility_score"] or 0.0)
                final_score = float(result.get("score", 0.0) or 0.0)
                shadow_score = final_score * 0.9 + utility * 0.1
                scored.append((rank, result, utility, shadow_score))
            shadow_order = {
                id(result): shadow_rank
                for shadow_rank, (_rank, result, _utility, _score) in enumerate(
                    sorted(scored, key=lambda item: (-item[3], item[0])), start=1
                )
            }
            for rank, result, utility, shadow_score in scored:
                shadow_rank = shadow_order[id(result)]
                conn.execute(
                    """
                    INSERT INTO retrieval_results (
                        retrieval_run_id, memory_id, rank, shadow_rank,
                        keyword_score, vector_score, graph_score, tier_boost,
                        utility_score, final_score, shadow_score, selected,
                        injected, used, scoring_policy_version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?)
                    """,
                    (
                        run_id,
                        result["id"],
                        rank,
                        shadow_rank,
                        result.get("keyword_score"),
                        result.get("vector_score"),
                        result.get("graph_score"),
                        result.get("tier_boost"),
                        utility,
                        float(result.get("score", 0.0) or 0.0),
                        shadow_score,
                        self.policy_version,
                        now,
                    ),
                )
                conn.execute(
                    "UPDATE memories SET last_retrieved_at = ? WHERE id = ?",
                    (now, result["id"]),
                )
                result.update(
                    {
                        "retrieval_run_id": run_id,
                        "production_rank": rank,
                        "shadow_rank": shadow_rank,
                        "utility_score": utility,
                        "shadow_score": shadow_score,
                        "utility_policy_version": self.policy_version,
                    }
                )
            return run_id

    def create_context_pack(
        self,
        retrieval_run_id: str,
        *,
        task_id: str | None,
        session_id: str | None,
        agent_id: str | None,
        token_budget: int | None,
        estimated_tokens: int,
    ) -> str:
        now = time.time()
        with self._connect(immediate=True) as conn:
            run = conn.execute(
                """
                SELECT task_id, session_id, agent_id FROM retrieval_runs
                WHERE id = ?
                """,
                (retrieval_run_id,),
            ).fetchone()
            if run is None:
                raise ContinuityError("retrieval run was not found", code="not_found")
            if any(
                requested != run[field]
                for field, requested in (
                    ("task_id", task_id),
                    ("session_id", session_id),
                    ("agent_id", agent_id),
                )
            ):
                raise ContinuityError(
                    "context pack scope does not match its retrieval run",
                    code="scope_mismatch",
                )
            existing = conn.execute(
                "SELECT id FROM context_packs WHERE retrieval_run_id = ?",
                (retrieval_run_id,),
            ).fetchone()
            if existing:
                return str(existing["id"])
            pack_id = _id("context")
            conn.execute(
                """
                INSERT INTO context_packs (
                    id, retrieval_run_id, task_id, session_id, agent_id,
                    token_budget, estimated_tokens, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pack_id,
                    retrieval_run_id,
                    task_id,
                    session_id,
                    agent_id,
                    token_budget,
                    max(0, estimated_tokens),
                    now,
                ),
            )
            rows = conn.execute(
                """
                SELECT memory_id FROM retrieval_results
                WHERE retrieval_run_id = ? ORDER BY rank
                """,
                (retrieval_run_id,),
            ).fetchall()
            conn.execute(
                """
                UPDATE retrieval_results SET selected = 1
                WHERE retrieval_run_id = ?
                """,
                (retrieval_run_id,),
            )
            for row in rows:
                memory_id = str(row["memory_id"])
                for usage_type in ("returned", "selected"):
                    conn.execute(
                        """
                        INSERT INTO context_usage (
                            id, context_pack_id, memory_id, agent_id, usage_type,
                            created_at, metadata_json, idempotency_key
                        ) VALUES (?, ?, ?, ?, ?, ?, '{}', ?)
                        """,
                        (
                            _id("usage"),
                            pack_id,
                            memory_id,
                            agent_id,
                            usage_type,
                            now,
                            f"system:{usage_type}:{memory_id}",
                        ),
                    )
                self._apply_usage_timestamp(
                    conn, memory_id, "selected", now=now, successful=False
                )
                self._persist_utility(
                    conn, memory_id, reason="context.selected", now=now
                )
            return pack_id

    def context_pack_for_run(self, retrieval_run_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM context_packs WHERE retrieval_run_id = ?",
                (retrieval_run_id,),
            ).fetchone()
        if row is None:
            raise ContinuityError("retrieval run has no context pack", code="not_found")
        return str(row["id"])

    def _apply_usage_timestamp(
        self,
        conn: sqlite3.Connection,
        memory_id: str,
        usage_type: str,
        *,
        now: float,
        successful: bool,
    ) -> None:
        timestamp_column = {
            "selected": "last_selected_at",
            "injected": "last_injected_at",
            "expanded": "last_expanded_at",
            "used": "last_used_at",
        }.get(usage_type)
        extension = {
            "selected": self.settings.RETENTION_SELECTION_SECONDS,
            "injected": self.settings.RETENTION_INJECTION_SECONDS,
            "expanded": self.settings.RETENTION_EXPANSION_SECONDS,
            "used": self.settings.RETENTION_USE_SECONDS,
        }.get(usage_type, 0)
        memory = self._memory(conn, memory_id)
        updates: list[str] = []
        params: list[object] = []
        if timestamp_column:
            updates.append(f"{timestamp_column} = ?")
            params.append(now)
        if successful:
            updates.append("last_successful_use_at = ?")
            params.append(now)
            extension = self.settings.RETENTION_SUCCESS_SECONDS
        if memory["pinned"]:
            updates.append("expires_at = NULL")
        elif extension > 0:
            current_expiry = float(memory["expires_at"] or 0.0)
            updates.append("expires_at = ?")
            params.append(max(current_expiry, now + extension))
        if usage_type in {"expanded", "used"} or successful:
            updates.append("retention_review_at = ?")
            params.append(now + self.settings.RETENTION_REVIEW_SECONDS)
        if updates:
            params.append(memory_id)
            conn.execute(
                f"UPDATE memories SET {', '.join(updates)} WHERE id = ?", params
            )

    def record_context_usage(
        self,
        *,
        context_pack_id: str,
        memory_id: str,
        agent_id: str | None,
        usage_type: str,
        metadata: Mapping[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        if usage_type not in USAGE_TYPES:
            raise ContinuityError("unsupported context usage type")
        now = time.time()
        with self._connect(immediate=True) as conn:
            self._ensure_agent(conn, agent_id, now=now)
            membership = conn.execute(
                """
                SELECT rr.retrieval_run_id
                FROM context_packs p
                JOIN retrieval_results rr
                  ON rr.retrieval_run_id = p.retrieval_run_id
                WHERE p.id = ? AND rr.memory_id = ?
                """,
                (context_pack_id, memory_id),
            ).fetchone()
            if membership is None:
                raise ContinuityError(
                    "memory is outside the context pack scope", code="not_found"
                )
            if idempotency_key:
                prior = conn.execute(
                    """
                    SELECT * FROM context_usage
                    WHERE context_pack_id = ? AND idempotency_key = ?
                    """,
                    (context_pack_id, idempotency_key),
                ).fetchone()
                if prior:
                    if (
                        prior["memory_id"] != memory_id
                        or prior["usage_type"] != usage_type
                        or json.loads(prior["metadata_json"] or "{}")
                        != dict(metadata or {})
                    ):
                        raise ContinuityError(
                            "idempotency key was reused with different feedback",
                            code="idempotency_conflict",
                        )
                    return {
                        "id": prior["id"],
                        "idempotent_replay": True,
                        "memory_id": memory_id,
                        "usage_type": usage_type,
                    }
            usage_id = _id("usage")
            conn.execute(
                """
                INSERT INTO context_usage (
                    id, context_pack_id, memory_id, agent_id, usage_type,
                    created_at, metadata_json, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    usage_id,
                    context_pack_id,
                    memory_id,
                    agent_id,
                    usage_type,
                    now,
                    _json(dict(metadata or {})),
                    idempotency_key,
                ),
            )
            flag = {"selected": "selected", "injected": "injected", "used": "used"}.get(
                usage_type
            )
            if flag:
                conn.execute(
                    f"""
                    UPDATE retrieval_results SET {flag} = 1
                    WHERE retrieval_run_id = ? AND memory_id = ?
                    """,
                    (membership["retrieval_run_id"], memory_id),
                )
            self._apply_usage_timestamp(
                conn, memory_id, usage_type, now=now, successful=False
            )
            score, components = self._persist_utility(
                conn, memory_id, reason=f"context.{usage_type}", now=now
            )
            return {
                "id": usage_id,
                "idempotent_replay": False,
                "memory_id": memory_id,
                "usage_type": usage_type,
                "utility_score": score,
                "components": components.as_dict(),
                "policy_version": self.policy_version,
            }

    def record_feedback(
        self,
        *,
        context_pack_id: str,
        agent_id: str | None,
        used_memory_ids: Sequence[str] = (),
        ignored_memory_ids: Sequence[str] = (),
        expanded_memory_ids: Sequence[str] = (),
        corrected_memory_ids: Sequence[str] = (),
        rejected_memory_ids: Sequence[str] = (),
        idempotency_key: str | None = None,
    ) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for usage_type, memory_ids in (
            ("used", used_memory_ids),
            ("ignored", ignored_memory_ids),
            ("expanded", expanded_memory_ids),
            ("corrected", corrected_memory_ids),
            ("rejected", rejected_memory_ids),
        ):
            for memory_id in memory_ids:
                derived_key = (
                    f"{idempotency_key}:{usage_type}:{memory_id}"
                    if idempotency_key
                    else None
                )
                results.append(
                    self.record_context_usage(
                        context_pack_id=context_pack_id,
                        memory_id=memory_id,
                        agent_id=agent_id,
                        usage_type=usage_type,
                        idempotency_key=derived_key,
                    )
                )
        return results

    def record_task_outcome(
        self,
        *,
        task_id: str,
        session_id: str | None,
        status: str,
        successful: bool,
        agent_id: str | None = None,
        user_correction_count: int = 0,
        rework_required: bool = False,
        metadata: Mapping[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        if user_correction_count < 0:
            raise ContinuityError("user_correction_count must not be negative")
        now = time.time()
        with self._connect(immediate=True) as conn:
            self._ensure_agent(conn, agent_id, now=now)
            task = conn.execute(
                "SELECT id FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise ContinuityError(f"task {task_id} was not found", code="not_found")
            if session_id:
                session = conn.execute(
                    "SELECT task_id FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if session is None or session["task_id"] != task_id:
                    raise ContinuityError(
                        "session is outside the requested task", code="scope_mismatch"
                    )
            if idempotency_key:
                prior = conn.execute(
                    """
                    SELECT * FROM task_outcomes
                    WHERE task_id = ? AND idempotency_key = ?
                    """,
                    (task_id, idempotency_key),
                ).fetchone()
                if prior:
                    canonical = (
                        session_id,
                        agent_id,
                        status,
                        int(successful),
                        user_correction_count,
                        int(rework_required),
                        _json(dict(metadata or {})),
                    )
                    existing = (
                        prior["session_id"],
                        prior["agent_id"],
                        prior["status"],
                        prior["successful"],
                        prior["user_correction_count"],
                        prior["rework_required"],
                        prior["metadata_json"],
                    )
                    if existing != canonical:
                        raise ContinuityError(
                            "idempotency key was reused with different task outcome",
                            code="idempotency_conflict",
                        )
                    result = dict(prior)
                    result["idempotent_replay"] = True
                    return result
            outcome_id = _id("outcome")
            conn.execute(
                """
                INSERT INTO task_outcomes (
                    id, task_id, session_id, agent_id, status, successful,
                    user_correction_count, rework_required, completed_at,
                    metadata_json, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    outcome_id,
                    task_id,
                    session_id,
                    agent_id,
                    status,
                    int(successful),
                    user_correction_count,
                    int(rework_required),
                    now,
                    _json(dict(metadata or {})),
                    idempotency_key,
                ),
            )
            used_rows = conn.execute(
                """
                SELECT DISTINCT u.memory_id
                FROM context_packs p
                JOIN context_usage u ON u.context_pack_id = p.id
                WHERE p.task_id = ?
                  AND (? IS NULL OR p.session_id = ?)
                  AND u.usage_type = 'used'
                """,
                (task_id, session_id, session_id),
            ).fetchall()
            for row in used_rows:
                memory_id = str(row["memory_id"])
                if successful:
                    self._apply_usage_timestamp(
                        conn, memory_id, "used", now=now, successful=True
                    )
                self._persist_utility(conn, memory_id, reason="task.outcome", now=now)
            return {
                "id": outcome_id,
                "task_id": task_id,
                "session_id": session_id,
                "agent_id": agent_id,
                "status": status,
                "successful": successful,
                "user_correction_count": user_correction_count,
                "rework_required": rework_required,
                "idempotent_replay": False,
            }

    def mark_context_injected(
        self, context_pack_id: str, *, agent_id: str | None
    ) -> list[dict[str, object]]:
        with self._connect() as conn:
            memory_ids = [
                str(row["memory_id"])
                for row in conn.execute(
                    """
                    SELECT rr.memory_id
                    FROM context_packs p
                    JOIN retrieval_results rr
                      ON rr.retrieval_run_id = p.retrieval_run_id
                    WHERE p.id = ? ORDER BY rr.rank
                    """,
                    (context_pack_id,),
                ).fetchall()
            ]
        return [
            self.record_context_usage(
                context_pack_id=context_pack_id,
                memory_id=memory_id,
                agent_id=agent_id,
                usage_type="injected",
                idempotency_key=f"system:injected:{memory_id}",
            )
            for memory_id in memory_ids
        ]

    def _transition(
        self,
        memory_id: str,
        *,
        new_state: str,
        reason: str,
        actor_id: str | None,
        pinned: bool | None = None,
        refresh_expiry: bool = False,
    ) -> dict[str, object]:
        allowed = {
            "pinned",
            "stable",
            "active",
            "session",
            "ephemeral",
            "cold",
            "archived",
        }
        if new_state not in allowed:
            raise ContinuityError("unsupported lifecycle state")
        now = time.time()
        with self._connect(immediate=True) as conn:
            self._ensure_agent(conn, actor_id, now=now)
            memory = self._memory(conn, memory_id)
            previous = str(memory["lifecycle_state"])
            pin_value = int(memory["pinned"] if pinned is None else pinned)
            expires_at = None if pin_value else memory["expires_at"]
            retention_review_at = memory["retention_review_at"]
            if refresh_expiry and not pin_value:
                expires_at = now + self.settings.RETENTION_REVIEW_SECONDS
                retention_review_at = expires_at
            conn.execute(
                """
                UPDATE memories
                SET lifecycle_state = ?, pinned = ?, expires_at = ?,
                    retention_review_at = ?, utility_policy_version = ?
                WHERE id = ?
                """,
                (
                    new_state,
                    pin_value,
                    expires_at,
                    retention_review_at,
                    self.policy_version,
                    memory_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO memory_lifecycle_audit (
                    id, memory_id, previous_state, new_state, reason,
                    actor_id, policy_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _id("lifecycle"),
                    memory_id,
                    previous,
                    new_state,
                    reason,
                    actor_id,
                    self.policy_version,
                    now,
                ),
            )
            score, components = self._persist_utility(
                conn, memory_id, reason=f"lifecycle.{reason}", now=now
            )
            return {
                "memory_id": memory_id,
                "previous_state": previous,
                "lifecycle_state": new_state,
                "pinned": bool(pin_value),
                "utility_score": score,
                "components": components.as_dict(),
                "policy_version": self.policy_version,
            }

    def demote_to_cold(
        self, memory_id: str, *, reason: str, actor_id: str | None = None
    ) -> dict[str, object]:
        return self._transition(
            memory_id,
            new_state="cold",
            reason=reason,
            actor_id=actor_id,
            pinned=False,
        )

    def restore_from_cold(
        self, memory_id: str, *, actor_id: str | None = None
    ) -> dict[str, object]:
        return self._transition(
            memory_id,
            new_state="active",
            reason="restore",
            actor_id=actor_id,
            refresh_expiry=True,
        )

    def pin(self, memory_id: str, *, actor_id: str | None = None) -> dict[str, object]:
        return self._transition(
            memory_id,
            new_state="pinned",
            reason="pin",
            actor_id=actor_id,
            pinned=True,
        )

    def unpin(
        self, memory_id: str, *, actor_id: str | None = None
    ) -> dict[str, object]:
        score, _components = self.calculate_utility(memory_id)
        return self._transition(
            memory_id,
            new_state="stable" if score >= 0.6 else "active",
            reason="unpin",
            actor_id=actor_id,
            pinned=False,
            refresh_expiry=True,
        )

    def review_expired(self, *, now: float | None = None) -> int:
        reviewed_at = time.time() if now is None else now
        with self._connect() as conn:
            ids = [
                str(row["id"])
                for row in conn.execute(
                    """
                    SELECT id FROM memories
                    WHERE pinned = 0
                      AND lifecycle_state NOT IN ('cold', 'archived')
                      AND expires_at IS NOT NULL AND expires_at <= ?
                    ORDER BY id
                    """,
                    (reviewed_at,),
                ).fetchall()
            ]
        for memory_id in ids:
            self.demote_to_cold(memory_id, reason="retention_review")
        return len(ids)

    def shadow_report(self, *, limit: int = 100) -> dict[str, object]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT rr.retrieval_run_id, rr.memory_id, rr.rank,
                       rr.shadow_rank, rr.utility_score, rr.final_score,
                       rr.shadow_score, m.lifecycle_state, m.pinned
                FROM retrieval_results rr
                JOIN memories m ON m.id = rr.memory_id
                ORDER BY rr.created_at DESC, rr.retrieval_run_id, rr.rank
                LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        entries: list[dict[str, object]] = []
        for row in rows:
            current = int(row["rank"])
            shadow = int(row["shadow_rank"] or current)
            if shadow < current:
                change = "promote"
            elif shadow > current:
                change = "demote"
            else:
                change = "unchanged"
            entries.append(
                {
                    **dict(row),
                    "change": change,
                    "rank_delta": current - shadow,
                    "cold_candidate": (
                        not bool(row["pinned"]) and float(row["utility_score"]) <= 0.1
                    ),
                    "reason": "10% capped shadow utility comparison",
                }
            )
        return {
            "policy_version": self.policy_version,
            "shadow_mode": self.settings.UTILITY_SHADOW_MODE,
            "production_ranking_changed": False,
            "entries": entries,
        }
