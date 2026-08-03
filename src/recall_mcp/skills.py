"""Provider-neutral, immutable Recall Skill application service."""

from __future__ import annotations

import hashlib
import json
import math
import re
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
from recall_mcp.store.migrations import CURRENT_SCHEMA_VERSION, run_migrations


SKILL_SCHEMA_VERSION = 1
SKILL_COMPILER_POLICY_VERSION = "skill-compiler-v1"
SKILL_STATUSES = {
    "candidate",
    "pending_approval",
    "approved",
    "rejected",
    "stale",
    "deprecated",
    "archived",
}
SKILL_USAGE_TYPES = {
    "selected",
    "injected",
    "referenced",
    "expanded",
    "used",
    "ignored",
    "corrected",
    "rejected",
}
SOURCE_TYPES = {
    "memory",
    "context_pack",
    "repository",
    "fact",
    "decision",
    "session_event",
    "tool_output",
    "documentation",
    "external",
}
SOURCE_RELATIONSHIPS = {
    "supports",
    "constrains",
    "supersedes",
    "contradicts",
    "derived_from",
}


def _stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _identifier(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _token_estimate(value: object) -> int:
    text = value if isinstance(value, str) else _stable_json(value)
    return 0 if not text else max(1, math.ceil(len(text.encode("utf-8")) / 4))


def _text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in ("statement", "text", "summary", "decision", "constraint"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return _stable_json(value)


@dataclass(frozen=True)
class SkillScope:
    user_id: str
    workspace_id: str
    project_id: str
    repository_id: str | None = None

    def validated(self) -> SkillScope:
        for name in ("user_id", "workspace_id", "project_id"):
            if not str(getattr(self, name)).strip():
                raise ContinuityError(f"{name} must be a non-empty string")
        return self

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


@dataclass(frozen=True)
class SkillProposal:
    scope: SkillScope
    slug: str
    title: str
    purpose: str
    created_by_agent_id: str
    sources: tuple[Mapping[str, object], ...]
    summary: str = ""
    instructions: tuple[object, ...] = ()
    facts: tuple[object, ...] = ()
    decisions: tuple[object, ...] = ()
    constraints: tuple[object, ...] = ()
    open_questions: tuple[object, ...] = ()
    expires_at: float | None = None
    retention_review_at: float | None = None
    idempotency_key: str | None = None
    compiler_model: str | None = None
    generated: bool = False


class SkillService:
    """Compile, review, retrieve, invalidate, and audit exact skill versions."""

    def __init__(self, db_path: Path, settings: Settings):
        self.db_path = Path(db_path)
        self.settings = settings
        self.compiler_policy_version = getattr(
            settings, "SKILL_COMPILER_POLICY_VERSION", SKILL_COMPILER_POLICY_VERSION
        )
        self._metrics_lock = threading.Lock()
        self._compile_failures_total = 0
        self._refresh_total = 0
        run_migrations(self.db_path)

    @contextmanager
    def _connect(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _ensure_agent(conn: sqlite3.Connection, agent_id: str) -> None:
        if not agent_id.strip():
            raise ContinuityError("created_by_agent_id must be a non-empty string")
        now = time.time()
        row = conn.execute("SELECT id FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if row:
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
    def _resolve_scope(conn: sqlite3.Connection, scope: SkillScope) -> None:
        scope.validated()
        project = conn.execute(
            """
            SELECT id FROM projects
            WHERE id = ? AND user_id = ? AND workspace_id = ?
            """,
            (scope.project_id, scope.user_id, scope.workspace_id),
        ).fetchone()
        if project is None:
            raise ContinuityError(
                "skill project was not found in the requested scope",
                code="scope_mismatch",
            )
        if scope.repository_id is not None:
            repository = conn.execute(
                """
                SELECT id FROM repositories
                WHERE id = ? AND user_id = ? AND workspace_id = ?
                  AND project_id = ?
                """,
                (
                    scope.repository_id,
                    scope.user_id,
                    scope.workspace_id,
                    scope.project_id,
                ),
            ).fetchone()
            if repository is None:
                raise ContinuityError(
                    "skill repository was not found in the requested scope",
                    code="scope_mismatch",
                )

    @staticmethod
    def _assert_scope(row: Mapping[str, object], scope: SkillScope) -> None:
        for field in ("user_id", "workspace_id", "project_id"):
            if row[field] != getattr(scope, field):
                raise ContinuityError(
                    "skill was not found in the requested scope", code="not_found"
                )
        repository_id = row["repository_id"]
        if repository_id is not None and repository_id != scope.repository_id:
            raise ContinuityError(
                "skill was not found in the requested scope", code="not_found"
            )

    @staticmethod
    def _skill_id(scope: SkillScope, slug: str) -> str:
        parts = ["skill", scope.project_id]
        if scope.repository_id:
            parts.append(scope.repository_id)
        parts.append(slug)
        return ":".join(parts)

    @staticmethod
    def _slug(value: str) -> str:
        slug = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,126}", slug):
            raise ContinuityError(
                "skill slug must use lowercase letters, numbers, dots, dashes, or underscores"
            )
        return slug

    @staticmethod
    def _scope_predicate(
        scope: SkillScope, *, prefix: str = ""
    ) -> tuple[str, list[object]]:
        column = lambda name: f"{prefix}{name}"  # noqa: E731
        predicate = (
            f"{column('user_id')} = ? AND {column('workspace_id')} = ? "
            f"AND {column('project_id')} = ?"
        )
        values: list[object] = [scope.user_id, scope.workspace_id, scope.project_id]
        if scope.repository_id is not None:
            predicate += f" AND {column('repository_id')} = ?"
            values.append(scope.repository_id)
        return predicate, values

    def _resolve_source(
        self,
        conn: sqlite3.Connection,
        scope: SkillScope,
        source: Mapping[str, object],
    ) -> dict[str, object]:
        source_type = str(source.get("source_type") or "").strip()
        if source_type not in SOURCE_TYPES:
            raise ContinuityError("unsupported skill source type")
        source_id = str(source.get("source_id") or source.get("id") or "").strip()
        if not source_id:
            raise ContinuityError("skill source_id must be a non-empty string")
        relationship = str(source.get("relationship") or "supports")
        if relationship not in SOURCE_RELATIONSHIPS:
            raise ContinuityError("unsupported skill source relationship")

        snapshot: dict[str, object]
        source_version: str | None = None
        if source_type == "memory":
            predicate, values = self._scope_predicate(scope)
            row = conn.execute(
                f"""
                SELECT id, content, summary, category, source, created_at,
                       accessed_at, user_id, workspace_id, project_id, repository_id
                FROM memories WHERE id = ? AND {predicate}
                """,
                (source_id, *values),
            ).fetchone()
            if row is None:
                raise ContinuityError(
                    "skill memory source was not found in scope", code="scope_mismatch"
                )
            snapshot = {
                "source_type": source_type,
                "source_id": source_id,
                "statement": row["summary"] or row["content"],
                "content": row["content"],
                "category": row["category"],
                "source_ref": row["source"],
                "created_at": row["created_at"],
                "accessed_at": row["accessed_at"],
                "verification": "memory_evidence",
            }
            source_version = str(row["accessed_at"])
        elif source_type == "context_pack":
            predicate, values = self._scope_predicate(scope)
            row = conn.execute(
                f"""
                SELECT id, pack_json, source_fingerprint, created_at
                FROM context_packs WHERE id = ? AND {predicate}
                """,
                (source_id, *values),
            ).fetchone()
            if row is None:
                raise ContinuityError(
                    "skill context-pack source was not found in scope",
                    code="scope_mismatch",
                )
            pack = json.loads(row["pack_json"])
            snapshot = {
                "source_type": source_type,
                "source_id": source_id,
                "statement": str(pack.get("inline_context") or "")[:1000],
                "source_fingerprint": row["source_fingerprint"],
                "created_at": row["created_at"],
                "verification": "compiled_context",
            }
            source_version = str(row["source_fingerprint"])
        elif source_type in {"decision", "session_event"}:
            row = conn.execute(
                """
                SELECT e.id, e.event_type, e.payload_json, e.sequence, e.created_at
                FROM session_events e
                JOIN sessions s ON s.id = e.session_id
                JOIN tasks t ON t.id = s.task_id
                WHERE e.id = ? AND t.user_id = ? AND t.workspace_id = ?
                  AND t.project_id = ?
                  AND (? IS NULL OR t.repository_id = ?)
                """,
                (
                    source_id,
                    scope.user_id,
                    scope.workspace_id,
                    scope.project_id,
                    scope.repository_id,
                    scope.repository_id,
                ),
            ).fetchone()
            if row is None or (
                source_type == "decision"
                and row["event_type"] not in {"decision.proposed", "decision.approved"}
            ):
                raise ContinuityError(
                    "skill session source was not found in scope", code="scope_mismatch"
                )
            payload = json.loads(row["payload_json"])
            snapshot = {
                "source_type": source_type,
                "source_id": source_id,
                "statement": _text(payload),
                "event_type": row["event_type"],
                "payload": payload,
                "verification": (
                    "user_approved"
                    if row["event_type"] == "decision.approved"
                    else "proposed"
                ),
                "created_at": row["created_at"],
            }
            source_version = str(row["sequence"])
        elif source_type == "repository":
            repository_id = str(source.get("repository_id") or source_id)
            row = conn.execute(
                """
                SELECT id, canonical_path, remote_url, default_branch, updated_at
                FROM repositories
                WHERE id = ? AND user_id = ? AND workspace_id = ? AND project_id = ?
                  AND (? IS NULL OR id = ?)
                """,
                (
                    repository_id,
                    scope.user_id,
                    scope.workspace_id,
                    scope.project_id,
                    scope.repository_id,
                    scope.repository_id,
                ),
            ).fetchone()
            if row is None:
                raise ContinuityError(
                    "skill repository source was not found in scope",
                    code="scope_mismatch",
                )
            content = str(source.get("content") or "").strip()
            source_ref = str(source.get("source_ref") or "").strip()
            if not content or not source_ref:
                raise ContinuityError(
                    "repository evidence requires source_ref and content"
                )
            snapshot = {
                "source_type": source_type,
                "source_id": repository_id,
                "source_ref": source_ref,
                "statement": str(source.get("statement") or content),
                "content": content,
                "branch": source.get("branch") or row["default_branch"],
                "commit_sha": source.get("commit_sha"),
                "verification": "repository_verified",
            }
            source_id = repository_id
            source_version = str(
                source.get("commit_sha")
                or source.get("source_version")
                or row["updated_at"]
            )
        else:
            source_scope = source.get("scope")
            if not isinstance(source_scope, Mapping):
                raise ContinuityError(f"{source_type} evidence requires explicit scope")
            for field in ("user_id", "workspace_id", "project_id"):
                if source_scope.get(field) != getattr(scope, field):
                    raise ContinuityError(
                        "skill source was not found in scope", code="scope_mismatch"
                    )
            source_repository = source_scope.get("repository_id")
            if (
                scope.repository_id is not None
                and source_repository != scope.repository_id
            ):
                raise ContinuityError(
                    "skill source was not found in scope", code="scope_mismatch"
                )
            content = str(
                source.get("content") or source.get("statement") or ""
            ).strip()
            if not content:
                raise ContinuityError("skill evidence content must not be empty")
            verification = {
                "tool_output": "successful_tool_output",
                "documentation": "versioned_documentation",
                "fact": "fact_record",
                "external": "external_unverified",
            }.get(source_type, "unverified")
            snapshot = {
                "source_type": source_type,
                "source_id": source_id,
                "source_ref": source.get("source_ref"),
                "statement": str(source.get("statement") or content),
                "content": content,
                "verification": verification,
                "scope": dict(source_scope),
            }
            source_version = str(source.get("source_version") or "") or None

        source_hash = _digest(snapshot)
        supplied_hash = source.get("source_hash")
        if supplied_hash and str(supplied_hash) != source_hash:
            raise ContinuityError("skill source hash does not match its evidence")
        return {
            "source_type": source_type,
            "source_id": source_id,
            "source_version": source_version,
            "source_hash": source_hash,
            "relationship": relationship,
            "source": snapshot,
        }

    @staticmethod
    def _derived_facts(sources: Sequence[Mapping[str, object]]) -> list[object]:
        facts: list[object] = []
        for source in sources:
            snapshot = source["source"]
            assert isinstance(snapshot, Mapping)
            facts.append(
                {
                    "statement": _text(snapshot.get("statement", "")),
                    "verification": snapshot.get("verification", "unverified"),
                    "source_type": source["source_type"],
                    "source_id": source["source_id"],
                }
            )
        return facts

    @staticmethod
    def _markdown(content: Mapping[str, object]) -> str:
        blocks = [f"# {str(content['title']).replace(chr(10), ' ')}"]
        purpose = str(content.get("purpose") or "")
        if purpose:
            blocks.extend(["## Purpose", purpose])
        summary = str(content.get("summary") or "")
        if summary:
            blocks.extend(["## Summary", summary])
        sections = (
            ("Instructions", "instructions"),
            ("Approved decisions and proposals", "decisions"),
            ("Current facts", "facts"),
            ("Constraints", "constraints"),
            ("Open questions", "open_questions"),
        )
        for heading, key in sections:
            values = content.get(key) or []
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
                continue
            if values:
                blocks.append(f"## {heading}")
                blocks.extend(f"- {_text(value)}" for value in values)
        evidence = content.get("evidence") or []
        if isinstance(evidence, Sequence) and evidence:
            blocks.append("## Evidence")
            for item in evidence:
                if isinstance(item, Mapping):
                    blocks.append(
                        f"- `{item.get('source_type')}:{item.get('source_id')}` "
                        f"({item.get('relationship')}) {item.get('source_hash')}"
                    )
        return "\n\n".join(blocks).strip() + "\n"

    def propose(self, proposal: SkillProposal) -> dict[str, Any]:
        try:
            return self._propose(proposal)
        except Exception:
            with self._metrics_lock:
                self._compile_failures_total += 1
            raise

    def _propose(self, proposal: SkillProposal) -> dict[str, Any]:
        scope = proposal.scope.validated()
        slug = self._slug(proposal.slug)
        title = proposal.title.strip()
        purpose = proposal.purpose.strip()
        if not title or not purpose:
            raise ContinuityError("skill title and purpose must not be empty")
        if not proposal.sources:
            raise ContinuityError(
                "skill candidates require at least one evidence source"
            )

        with self._connect(immediate=True) as conn:
            self._resolve_scope(conn, scope)
            self._ensure_agent(conn, proposal.created_by_agent_id)
            resolved_sources = [
                self._resolve_source(conn, scope, source) for source in proposal.sources
            ]
            facts = list(proposal.facts) or self._derived_facts(resolved_sources)
            evidence = [
                {
                    key: source[key]
                    for key in (
                        "source_type",
                        "source_id",
                        "source_version",
                        "source_hash",
                        "relationship",
                    )
                }
                for source in resolved_sources
            ]
            content: dict[str, object] = {
                "schema_version": SKILL_SCHEMA_VERSION,
                "title": title,
                "purpose": purpose,
                "summary": proposal.summary.strip(),
                "instructions": list(proposal.instructions),
                "facts": facts,
                "decisions": list(proposal.decisions),
                "constraints": list(proposal.constraints),
                "open_questions": list(proposal.open_questions),
                "evidence": evidence,
                "metadata": {
                    "compiler_policy_version": self.compiler_policy_version,
                    "compiler_model": proposal.compiler_model,
                    "generated": proposal.generated,
                },
            }
            content_hash = _digest(content)
            source_fingerprint = _digest(
                {
                    "schema_version": SKILL_SCHEMA_VERSION,
                    "compiler_policy_version": self.compiler_policy_version,
                    "sources": evidence,
                }
            )
            markdown = self._markdown(content)
            token_estimate = _token_estimate(markdown)
            skill_id = self._skill_id(scope, slug)

            if proposal.idempotency_key:
                replay = conn.execute(
                    "SELECT * FROM skills WHERE user_id = ? AND idempotency_key = ?",
                    (scope.user_id, proposal.idempotency_key),
                ).fetchone()
                if replay is not None:
                    self._assert_scope(replay, scope)
                    latest = conn.execute(
                        """
                        SELECT content_hash, source_fingerprint FROM skill_versions
                        WHERE skill_id = ? AND version = ?
                        """,
                        (replay["id"], replay["current_version"]),
                    ).fetchone()
                    assert latest is not None
                    if (
                        latest["content_hash"] != content_hash
                        or latest["source_fingerprint"] != source_fingerprint
                    ):
                        raise ContinuityError(
                            "idempotency key was reused with different skill input",
                            code="idempotency_conflict",
                        )
                    result = self._get_with_conn(
                        conn, str(replay["id"]), int(replay["current_version"]), scope
                    )
                    result["idempotent_replay"] = True
                    return result

            skill = conn.execute(
                """
                SELECT * FROM skills
                WHERE user_id = ? AND workspace_id = ? AND project_id = ?
                  AND repository_id IS ? AND slug = ?
                """,
                (
                    scope.user_id,
                    scope.workspace_id,
                    scope.project_id,
                    scope.repository_id,
                    slug,
                ),
            ).fetchone()
            now = time.time()
            if skill is None:
                conn.execute(
                    """
                    INSERT INTO skills (
                        id, user_id, workspace_id, project_id, repository_id,
                        slug, title, purpose, status, current_version,
                        current_approved_version, created_by_agent_id,
                        idempotency_key, created_at, updated_at, expires_at,
                        retention_review_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'candidate', 0, NULL, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        skill_id,
                        scope.user_id,
                        scope.workspace_id,
                        scope.project_id,
                        scope.repository_id,
                        slug,
                        title,
                        purpose,
                        proposal.created_by_agent_id,
                        proposal.idempotency_key,
                        now,
                        now,
                        proposal.expires_at,
                        proposal.retention_review_at,
                    ),
                )
                version = 1
            else:
                skill_id = str(skill["id"])
                version = int(skill["current_version"]) + 1
                latest = conn.execute(
                    """
                    SELECT content_hash, source_fingerprint FROM skill_versions
                    WHERE skill_id = ? AND version = ?
                    """,
                    (skill_id, skill["current_version"]),
                ).fetchone()
                if (
                    latest
                    and latest["content_hash"] == content_hash
                    and latest["source_fingerprint"] == source_fingerprint
                ):
                    result = self._get_with_conn(
                        conn, skill_id, int(skill["current_version"]), scope
                    )
                    result["idempotent_replay"] = True
                    return result

            conn.execute(
                """
                INSERT INTO skill_versions (
                    skill_id, version, schema_version, content_json,
                    content_markdown, content_hash, source_fingerprint,
                    token_estimate, compiler_policy_version, compiler_model,
                    generated, created_by_agent_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    skill_id,
                    version,
                    SKILL_SCHEMA_VERSION,
                    _stable_json(content),
                    markdown,
                    content_hash,
                    source_fingerprint,
                    token_estimate,
                    self.compiler_policy_version,
                    proposal.compiler_model,
                    int(proposal.generated),
                    proposal.created_by_agent_id,
                    now,
                ),
            )
            for source in resolved_sources:
                conn.execute(
                    """
                    INSERT INTO skill_sources (
                        skill_id, skill_version, source_type, source_id,
                        source_version, source_hash, relationship, source_json,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        skill_id,
                        version,
                        source["source_type"],
                        source["source_id"],
                        source["source_version"],
                        source["source_hash"],
                        source["relationship"],
                        _stable_json(source["source"]),
                        now,
                    ),
                )
            conn.execute(
                """
                UPDATE skills SET current_version = ?, title = ?, purpose = ?,
                    status = 'pending_approval', updated_at = ?, expires_at = ?,
                    retention_review_at = ?
                WHERE id = ?
                """,
                (
                    version,
                    title,
                    purpose,
                    now,
                    proposal.expires_at,
                    proposal.retention_review_at,
                    skill_id,
                ),
            )
            result = self._get_with_conn(conn, skill_id, version, scope)
            result["idempotent_replay"] = False
            return result

    @staticmethod
    def _review_status(conn: sqlite3.Connection, skill_id: str, version: int) -> str:
        row = conn.execute(
            """
            SELECT decision FROM skill_reviews
            WHERE skill_id = ? AND skill_version = ?
            ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            (skill_id, version),
        ).fetchone()
        return str(row["decision"]) if row else "pending_approval"

    def _get_with_conn(
        self,
        conn: sqlite3.Connection,
        skill_id: str,
        version: int | None,
        scope: SkillScope,
    ) -> dict[str, Any]:
        skill = conn.execute(
            "SELECT * FROM skills WHERE id = ?", (skill_id,)
        ).fetchone()
        if skill is None:
            raise ContinuityError("skill was not found", code="not_found")
        self._assert_scope(skill, scope)
        selected_version = version
        if selected_version is None:
            selected_version = int(
                skill["current_approved_version"] or skill["current_version"]
            )
        row = conn.execute(
            """
            SELECT * FROM skill_versions WHERE skill_id = ? AND version = ?
            """,
            (skill_id, selected_version),
        ).fetchone()
        if row is None:
            raise ContinuityError("skill version was not found", code="not_found")
        content = json.loads(row["content_json"])
        return {
            "id": skill_id,
            "version": int(row["version"]),
            "schema_version": int(row["schema_version"]),
            "scope": {
                key: skill[key]
                for key in ("user_id", "workspace_id", "project_id", "repository_id")
                if skill[key] is not None
            },
            "slug": skill["slug"],
            "status": skill["status"],
            "version_status": self._review_status(conn, skill_id, int(row["version"])),
            "current_version": int(skill["current_version"]),
            "current_approved_version": skill["current_approved_version"],
            "title": skill["title"],
            "purpose": skill["purpose"],
            "content": content,
            "content_markdown": row["content_markdown"],
            "content_hash": row["content_hash"],
            "source_fingerprint": row["source_fingerprint"],
            "token_estimate": int(row["token_estimate"]),
            "compiler_policy_version": row["compiler_policy_version"],
            "compiler_model": row["compiler_model"],
            "generated": bool(row["generated"]),
            "created_by_agent_id": row["created_by_agent_id"],
            "created_at": row["created_at"],
            "expires_at": skill["expires_at"],
            "retention_review_at": skill["retention_review_at"],
            "stale_at": skill["stale_at"],
            "stale_reason": skill["stale_reason"],
        }

    def get(
        self, skill_id: str, *, scope: SkillScope, version: int | None = None
    ) -> dict[str, Any]:
        with self._connect() as conn:
            self._resolve_scope(conn, scope)
            return self._get_with_conn(conn, skill_id, version, scope)

    def versions(self, skill_id: str, *, scope: SkillScope) -> list[dict[str, Any]]:
        with self._connect() as conn:
            skill = conn.execute(
                "SELECT * FROM skills WHERE id = ?", (skill_id,)
            ).fetchone()
            if skill is None:
                raise ContinuityError("skill was not found", code="not_found")
            self._assert_scope(skill, scope)
            rows = conn.execute(
                "SELECT version FROM skill_versions WHERE skill_id = ? ORDER BY version",
                (skill_id,),
            ).fetchall()
            return [
                self._get_with_conn(conn, skill_id, int(row["version"]), scope)
                for row in rows
            ]

    def list_skills(
        self, *, scope: SkillScope, status: str | None = None
    ) -> list[dict[str, Any]]:
        if status is not None and status not in SKILL_STATUSES:
            raise ContinuityError("unsupported skill status")
        with self._connect() as conn:
            self._resolve_scope(conn, scope)
            query = """
                SELECT id FROM skills
                WHERE user_id = ? AND workspace_id = ? AND project_id = ?
                  AND (repository_id IS NULL OR repository_id = ?)
            """
            values: list[object] = [
                scope.user_id,
                scope.workspace_id,
                scope.project_id,
                scope.repository_id,
            ]
            if status:
                query += " AND status = ?"
                values.append(status)
            query += " ORDER BY updated_at DESC, id"
            rows = conn.execute(query, values).fetchall()
            return [
                self._get_with_conn(conn, str(row["id"]), None, scope) for row in rows
            ]

    @staticmethod
    def _authorize_reviewer(
        conn: sqlite3.Connection, skill: sqlite3.Row, reviewer_id: str
    ) -> None:
        if reviewer_id == skill["user_id"]:
            return
        agent = conn.execute(
            "SELECT trust_level FROM agents WHERE id = ?", (reviewer_id,)
        ).fetchone()
        if agent is None or agent["trust_level"] not in {"trusted", "high", "owner"}:
            raise ContinuityError("reviewer is not authorized", code="forbidden")

    def review(
        self,
        skill_id: str,
        version: int,
        *,
        scope: SkillScope,
        decision: str,
        reviewer_id: str,
        reason: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if decision not in {
            "approved",
            "rejected",
            "request_changes",
            "deprecated",
            "archived",
        }:
            raise ContinuityError("unsupported skill review decision")
        with self._connect(immediate=True) as conn:
            skill = conn.execute(
                "SELECT * FROM skills WHERE id = ?", (skill_id,)
            ).fetchone()
            if skill is None:
                raise ContinuityError("skill was not found", code="not_found")
            self._assert_scope(skill, scope)
            self._authorize_reviewer(conn, skill, reviewer_id)
            version_row = conn.execute(
                "SELECT * FROM skill_versions WHERE skill_id = ? AND version = ?",
                (skill_id, version),
            ).fetchone()
            if version_row is None:
                raise ContinuityError("skill version was not found", code="not_found")
            if idempotency_key:
                prior = conn.execute(
                    """
                    SELECT decision, reason FROM skill_reviews
                    WHERE skill_id = ? AND skill_version = ? AND idempotency_key = ?
                    """,
                    (skill_id, version, idempotency_key),
                ).fetchone()
                if prior:
                    if prior["decision"] != decision or prior["reason"] != reason:
                        raise ContinuityError(
                            "idempotency key was reused with a different review",
                            code="idempotency_conflict",
                        )
                    result = self._get_with_conn(conn, skill_id, version, scope)
                    result["idempotent_replay"] = True
                    return result
            latest = self._review_status(conn, skill_id, version)
            if latest in {"approved", "rejected", "deprecated", "archived"}:
                if latest == decision:
                    result = self._get_with_conn(conn, skill_id, version, scope)
                    result["idempotent_replay"] = True
                    return result
                raise ContinuityError(
                    "skill version already has a final review",
                    code="invalid_transition",
                )
            now = time.time()
            conn.execute(
                """
                INSERT INTO skill_reviews (
                    id, skill_id, skill_version, decision, reviewer_id,
                    reason, idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _identifier("review"),
                    skill_id,
                    version,
                    decision,
                    reviewer_id,
                    reason.strip(),
                    idempotency_key,
                    now,
                ),
            )
            status = decision
            approved_version = skill["current_approved_version"]
            stale_at = skill["stale_at"]
            stale_reason = skill["stale_reason"]
            if decision == "approved":
                approved_version = version
                status = "approved"
                stale_at = None
                stale_reason = None
            elif decision == "request_changes":
                status = "pending_approval"
            conn.execute(
                """
                UPDATE skills SET status = ?, current_approved_version = ?,
                    stale_at = ?, stale_reason = ?, updated_at = ? WHERE id = ?
                """,
                (status, approved_version, stale_at, stale_reason, now, skill_id),
            )
            result = self._get_with_conn(conn, skill_id, version, scope)
            result["idempotent_replay"] = False
            return result

    def approve(
        self,
        skill_id: str,
        version: int,
        *,
        scope: SkillScope,
        reviewer_id: str,
        reason: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self.review(
            skill_id,
            version,
            scope=scope,
            decision="approved",
            reviewer_id=reviewer_id,
            reason=reason,
            idempotency_key=idempotency_key,
        )

    def reject(
        self,
        skill_id: str,
        version: int,
        *,
        scope: SkillScope,
        reviewer_id: str,
        reason: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if not reason.strip():
            raise ContinuityError("skill rejection requires a reason")
        return self.review(
            skill_id,
            version,
            scope=scope,
            decision="rejected",
            reviewer_id=reviewer_id,
            reason=reason,
            idempotency_key=idempotency_key,
        )

    def mark_stale(
        self,
        skill_id: str,
        *,
        scope: SkillScope,
        reason: str = "manual_invalidation",
    ) -> dict[str, Any]:
        allowed = {
            "repository_changed",
            "source_memory_updated",
            "decision_superseded",
            "fact_contradicted",
            "dependency_updated",
            "manual_invalidation",
            "policy_changed",
        }
        if reason not in allowed:
            raise ContinuityError("unsupported stale reason")
        with self._connect(immediate=True) as conn:
            skill = conn.execute(
                "SELECT * FROM skills WHERE id = ?", (skill_id,)
            ).fetchone()
            if skill is None:
                raise ContinuityError("skill was not found", code="not_found")
            self._assert_scope(skill, scope)
            now = time.time()
            conn.execute(
                """
                UPDATE skills SET status = 'stale', stale_at = ?, stale_reason = ?,
                    updated_at = ? WHERE id = ?
                """,
                (now, reason, now, skill_id),
            )
            return self._get_with_conn(
                conn, skill_id, int(skill["current_version"]), scope
            )

    def refresh(
        self,
        skill_id: str,
        *,
        scope: SkillScope,
        created_by_agent_id: str,
        source_overrides: Sequence[Mapping[str, object]] | None = None,
    ) -> dict[str, Any]:
        with self._metrics_lock:
            self._refresh_total += 1
        current = self.get(skill_id, scope=scope, version=None)
        evidence = self.evidence(skill_id, current["current_version"], scope=scope)
        sources: Sequence[Mapping[str, object]] = source_overrides or [
            {
                "source_type": item["source_type"],
                "source_id": item["source_id"],
                "source_version": item.get("source_version"),
                "relationship": item["relationship"],
                **dict(item["source"]),
            }
            for item in evidence
        ]
        content = current["content"]
        assert isinstance(content, Mapping)
        result = self.propose(
            SkillProposal(
                scope=scope,
                slug=str(current["slug"]),
                title=str(current["title"]),
                purpose=str(current["purpose"]),
                created_by_agent_id=created_by_agent_id,
                sources=tuple(sources),
                summary=str(content.get("summary") or ""),
                instructions=tuple(content.get("instructions") or ()),
                facts=tuple(content.get("facts") or ()),
                decisions=tuple(content.get("decisions") or ()),
                constraints=tuple(content.get("constraints") or ()),
                open_questions=tuple(content.get("open_questions") or ()),
                expires_at=current.get("expires_at"),
                retention_review_at=current.get("retention_review_at"),
            )
        )
        if int(result["version"]) > int(current["current_version"]):
            return self.mark_stale(
                skill_id,
                scope=scope,
                reason="source_memory_updated"
                if any(item["source_type"] == "memory" for item in evidence)
                else "repository_changed",
            ) | {"candidate_version": result}
        return result | {"refresh_status": "current"}

    def evidence(
        self, skill_id: str, version: int, *, scope: SkillScope
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            self._get_with_conn(conn, skill_id, version, scope)
            rows = conn.execute(
                """
                SELECT * FROM skill_sources
                WHERE skill_id = ? AND skill_version = ?
                ORDER BY source_type, source_id, relationship
                """,
                (skill_id, version),
            ).fetchall()
            return [
                {
                    "source_type": row["source_type"],
                    "source_id": row["source_id"],
                    "source_version": row["source_version"],
                    "source_hash": row["source_hash"],
                    "relationship": row["relationship"],
                    "source": json.loads(row["source_json"]),
                }
                for row in rows
            ]

    def explain(
        self, skill_id: str, *, scope: SkillScope, version: int | None = None
    ) -> dict[str, Any]:
        skill = self.get(skill_id, scope=scope, version=version)
        exact_version = int(skill["version"])
        evidence = self.evidence(skill_id, exact_version, scope=scope)
        return {
            "skill_id": skill_id,
            "version": exact_version,
            "status": skill["version_status"],
            "scope_filters": scope.as_dict(),
            "compiler_policy_version": skill["compiler_policy_version"],
            "content_hash": skill["content_hash"],
            "source_fingerprint": skill["source_fingerprint"],
            "stale": skill["stale_at"] is not None,
            "stale_reason": skill["stale_reason"],
            "contradictions": [
                item for item in evidence if item["relationship"] == "contradicts"
            ],
            "evidence": evidence,
        }

    def select_for_context(
        self,
        *,
        scope: SkillScope,
        objective: str,
        token_limit: int,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            self._resolve_scope(conn, scope)
            rows = conn.execute(
                """
                SELECT s.*, v.token_estimate, v.content_json, v.content_markdown,
                       v.content_hash, v.source_fingerprint,
                       COALESCE(SUM(CASE WHEN u.usage_type = 'used' THEN 1 ELSE 0 END), 0)
                           AS successful_uses
                FROM skills s
                JOIN skill_versions v
                  ON v.skill_id = s.id AND v.version = s.current_approved_version
                LEFT JOIN skill_usage u
                  ON u.skill_id = v.skill_id AND u.skill_version = v.version
                WHERE s.user_id = ? AND s.workspace_id = ? AND s.project_id = ?
                  AND (s.repository_id IS NULL OR s.repository_id = ?)
                  AND s.current_approved_version IS NOT NULL
                  AND s.stale_at IS NULL
                  AND s.status NOT IN ('deprecated', 'archived', 'stale')
                GROUP BY s.id, v.version
                """,
                (
                    scope.user_id,
                    scope.workspace_id,
                    scope.project_id,
                    scope.repository_id,
                ),
            ).fetchall()
        terms = {
            term
            for term in re.findall(r"[a-z0-9_]+", objective.lower())
            if len(term) > 2
        }
        ranked: list[tuple[tuple[int, int, int, str], sqlite3.Row]] = []
        for row in rows:
            haystack = f"{row['title']} {row['purpose']} {row['content_json']}".lower()
            overlap = sum(1 for term in terms if term in haystack)
            repository_match = int(
                row["repository_id"] is not None
                and row["repository_id"] == scope.repository_id
            )
            ranked.append(
                (
                    (
                        repository_match,
                        overlap,
                        int(row["successful_uses"]),
                        str(row["id"]),
                    ),
                    row,
                )
            )
        ranked.sort(key=lambda item: item[0], reverse=True)
        selected: list[dict[str, Any]] = []
        used_tokens = 0
        for _score, row in ranked:
            tokens = int(row["token_estimate"])
            if used_tokens + tokens > max(0, token_limit):
                continue
            used_tokens += tokens
            selected.append(
                {
                    "id": row["id"],
                    "version": int(row["current_approved_version"]),
                    "status": "approved",
                    "title": row["title"],
                    "purpose": row["purpose"],
                    "summary": json.loads(row["content_json"]).get("summary", ""),
                    "content_markdown": row["content_markdown"],
                    "token_estimate": tokens,
                    "content_hash": row["content_hash"],
                    "source_fingerprint": row["source_fingerprint"],
                    "successful_uses": int(row["successful_uses"]),
                }
            )
        return selected

    def record_usage(
        self,
        skill_id: str,
        version: int,
        *,
        scope: SkillScope,
        usage_type: str,
        agent_id: str | None = None,
        context_pack_id: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        metadata: Mapping[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        if usage_type not in SKILL_USAGE_TYPES:
            raise ContinuityError("unsupported skill usage type")
        with self._connect(immediate=True) as conn:
            self._get_with_conn(conn, skill_id, version, scope)
            if context_pack_id:
                predicate, values = self._scope_predicate(scope)
                pack = conn.execute(
                    f"SELECT id FROM context_packs WHERE id = ? AND {predicate}",
                    (context_pack_id, *values),
                ).fetchone()
                if pack is None:
                    raise ContinuityError(
                        "context pack was not found in skill scope",
                        code="scope_mismatch",
                    )
            if agent_id:
                self._ensure_agent(conn, agent_id)
            if idempotency_key:
                prior = conn.execute(
                    """
                    SELECT * FROM skill_usage
                    WHERE skill_id = ? AND skill_version = ? AND idempotency_key = ?
                    """,
                    (skill_id, version, idempotency_key),
                ).fetchone()
                if prior:
                    if prior["usage_type"] != usage_type:
                        raise ContinuityError(
                            "idempotency key was reused with different skill feedback",
                            code="idempotency_conflict",
                        )
                    return {"id": prior["id"], "idempotent_replay": True}
            usage_id = _identifier("skill_usage")
            conn.execute(
                """
                INSERT INTO skill_usage (
                    id, skill_id, skill_version, context_pack_id, task_id,
                    session_id, agent_id, usage_type, metadata_json,
                    idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    usage_id,
                    skill_id,
                    version,
                    context_pack_id,
                    task_id,
                    session_id,
                    agent_id,
                    usage_type,
                    _stable_json(dict(metadata or {})),
                    idempotency_key,
                    time.time(),
                ),
            )
            return {"id": usage_id, "idempotent_replay": False}

    def expand_evidence(
        self,
        skill_id: str,
        version: int,
        *,
        scope: SkillScope,
        agent_id: str | None = None,
        context_pack_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        evidence = self.evidence(skill_id, version, scope=scope)
        self.record_usage(
            skill_id,
            version,
            scope=scope,
            usage_type="expanded",
            agent_id=agent_id,
            context_pack_id=context_pack_id,
            idempotency_key=idempotency_key
            or f"expand:{context_pack_id or 'direct'}:{agent_id or 'anonymous'}",
        )
        return {"skill_id": skill_id, "version": version, "evidence": evidence}

    def render_json(
        self, skill_id: str, *, scope: SkillScope, version: int | None = None
    ) -> dict[str, Any]:
        return self.get(skill_id, scope=scope, version=version)

    def render_markdown(
        self, skill_id: str, *, scope: SkillScope, version: int | None = None
    ) -> str:
        return str(self.get(skill_id, scope=scope, version=version)["content_markdown"])

    def stats(self) -> dict[str, object]:
        with self._connect() as conn:
            counts = {
                row["status"]: int(row["count"])
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS count FROM skills GROUP BY status"
                ).fetchall()
            }
            versions = int(
                conn.execute("SELECT COUNT(*) FROM skill_versions").fetchone()[0]
            )
            approvals = int(
                conn.execute(
                    "SELECT COUNT(*) FROM skill_reviews WHERE decision = 'approved'"
                ).fetchone()[0]
            )
            rejections = int(
                conn.execute(
                    "SELECT COUNT(*) FROM skill_reviews WHERE decision = 'rejected'"
                ).fetchone()[0]
            )
            injections = int(
                conn.execute(
                    "SELECT COUNT(*) FROM skill_usage WHERE usage_type = 'injected'"
                ).fetchone()[0]
            )
            expansions = int(
                conn.execute(
                    "SELECT COUNT(*) FROM skill_usage WHERE usage_type = 'expanded'"
                ).fetchone()[0]
            )
        return {
            "schema_version": SKILL_SCHEMA_VERSION,
            "migration_version": CURRENT_SCHEMA_VERSION,
            "compiler_policy_version": self.compiler_policy_version,
            "total": sum(counts.values()),
            "versions_total": versions,
            "counts": counts,
            "pending_approval": counts.get("pending_approval", 0),
            "stale": counts.get("stale", 0),
            "approvals_total": approvals,
            "rejections_total": rejections,
            "context_injections_total": injections,
            "reference_expansions_total": expansions,
            "refresh_total": self._refresh_total,
            "compile_failures_total": self._compile_failures_total,
        }

    def health(self) -> dict[str, object]:
        try:
            stats = self.stats()
            return {
                "available": True,
                "schema_version": stats["schema_version"],
                "migration_current": stats["migration_version"]
                == CURRENT_SCHEMA_VERSION,
                "pending_approval": stats["pending_approval"],
                "stale": stats["stale"],
                "compiler_policy_version": self.compiler_policy_version,
            }
        except sqlite3.Error as exc:
            return {
                "available": False,
                "error": f"{type(exc).__name__}: skill health check failed",
                "compiler_policy_version": self.compiler_policy_version,
            }


class SkillCandidateService:
    """Named deterministic candidate-generation facade for integrations."""

    def __init__(self, skills: SkillService):
        self.skills = skills

    def propose_from_memories(self, proposal: SkillProposal) -> dict[str, Any]:
        if any(source.get("source_type") != "memory" for source in proposal.sources):
            raise ContinuityError("memory candidate sources must all be memories")
        return self.skills.propose(proposal)

    def propose_from_context_packs(self, proposal: SkillProposal) -> dict[str, Any]:
        if any(
            source.get("source_type") != "context_pack" for source in proposal.sources
        ):
            raise ContinuityError(
                "context-pack candidate sources must all be context packs"
            )
        return self.skills.propose(proposal)

    def propose_from_repository_evidence(
        self, proposal: SkillProposal
    ) -> dict[str, Any]:
        if any(
            source.get("source_type") != "repository" for source in proposal.sources
        ):
            raise ContinuityError(
                "repository candidate sources must all be repository evidence"
            )
        return self.skills.propose(proposal)

    def refresh_existing_skill(
        self,
        skill_id: str,
        *,
        scope: SkillScope,
        created_by_agent_id: str,
        source_overrides: Sequence[Mapping[str, object]] | None = None,
    ) -> dict[str, Any]:
        return self.skills.refresh(
            skill_id,
            scope=scope,
            created_by_agent_id=created_by_agent_id,
            source_overrides=source_overrides,
        )
