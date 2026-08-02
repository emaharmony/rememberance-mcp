"""Ordered, transactional SQLite schema migrations for Recall."""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


class SchemaMigrationError(RuntimeError):
    """Raised when Recall cannot safely establish the supported schema."""


@dataclass(frozen=True)
class Migration:
    """One immutable schema migration."""

    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class AppliedMigration:
    """A migration applied by the current runner invocation."""

    version: int
    name: str

    def as_dict(self) -> dict[str, int | str]:
        return {"version": self.version, "name": self.name}


@dataclass(frozen=True)
class MigrationResult:
    """Observable result of bringing a database to the current schema."""

    database: Path
    from_version: int
    to_version: int
    applied: tuple[AppliedMigration, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "database": str(self.database),
            "from_version": self.from_version,
            "to_version": self.to_version,
            "applied": [migration.as_dict() for migration in self.applied],
        }


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _require_columns(conn: sqlite3.Connection, table: str, required: set[str]) -> None:
    columns = _table_columns(conn, table)
    if not columns:
        raise SchemaMigrationError(f"required table is missing: {table}")
    missing = required - columns
    if missing:
        names = ", ".join(sorted(missing))
        raise SchemaMigrationError(f"{table} is missing required columns: {names}")


def _add_columns(
    conn: sqlite3.Connection, table: str, definitions: dict[str, str]
) -> None:
    existing = _table_columns(conn, table)
    if not existing:
        raise SchemaMigrationError(f"cannot add columns to missing table: {table}")
    for column, definition in definitions.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _execute_statements(conn: sqlite3.Connection, script: str) -> None:
    """Execute a multi-statement script without sqlite3.executescript auto-commit."""
    pending = ""
    for line in script.splitlines():
        pending += line + "\n"
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            if statement:
                conn.execute(statement)
            pending = ""
    if pending.strip():
        raise SchemaMigrationError("incomplete SQL statement in migration")


def _migration_core_memory(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            summary TEXT,
            category TEXT NOT NULL DEFAULT 'project',
            tier TEXT NOT NULL DEFAULT 'active',
            key_topics TEXT,
            source TEXT DEFAULT '',
            embedding BLOB,
            created_at REAL NOT NULL,
            accessed_at REAL NOT NULL,
            expires_at REAL,
            UNIQUE(id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_tier ON memories(tier)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category)"
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memories_expires
        ON memories(expires_at) WHERE expires_at IS NOT NULL
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_accessed ON memories(accessed_at)"
    )
    _require_columns(
        conn,
        "memories",
        {
            "id",
            "content",
            "summary",
            "category",
            "tier",
            "key_topics",
            "source",
            "embedding",
            "created_at",
            "accessed_at",
            "expires_at",
        },
    )


def _migration_memory_v2(conn: sqlite3.Connection) -> None:
    _add_columns(
        conn,
        "memories",
        {
            "compiled_truth": "TEXT DEFAULT ''",
            "timeline": "TEXT DEFAULT ''",
            "dream_count": "INTEGER DEFAULT 0",
            "last_dream_at": "REAL",
        },
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dream_log (
            id TEXT PRIMARY KEY,
            started_at REAL NOT NULL,
            completed_at REAL,
            status TEXT NOT NULL,
            phases_run TEXT,
            totals TEXT,
            error TEXT
        )
        """
    )
    _require_columns(
        conn,
        "memories",
        {"compiled_truth", "timeline", "dream_count", "last_dream_at"},
    )


def _migration_graph_and_facts(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS entities (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            aliases TEXT DEFAULT '[]',
            compiled_truth TEXT DEFAULT '',
            timeline TEXT DEFAULT '',
            tier TEXT NOT NULL DEFAULT 'active',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS edges (
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            edge_type TEXT NOT NULL,
            since_date REAL NOT NULL,
            confidence REAL DEFAULT 1.0,
            evidence TEXT DEFAULT '',
            UNIQUE(source_id, target_id, edge_type),
            FOREIGN KEY (source_id) REFERENCES entities(id) ON DELETE CASCADE,
            FOREIGN KEY (target_id) REFERENCES entities(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_entities (
            memory_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            confidence REAL DEFAULT 1.0,
            UNIQUE(memory_id, entity_id),
            FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE,
            FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS entity_aliases (
            alias TEXT NOT NULL,
            entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            UNIQUE(alias, entity_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS facts (
            id TEXT PRIMARY KEY,
            entity_id TEXT NOT NULL,
            claim_key TEXT NOT NULL,
            claim_value TEXT NOT NULL,
            source TEXT NOT NULL,
            confidence REAL DEFAULT 1.0,
            observed_at REAL NOT NULL,
            superseded_at REAL,
            UNIQUE(entity_id, claim_key, observed_at),
            FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE
        )
        """
    )

    indexes = (
        "CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id)",
        "CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id)",
        "CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(edge_type)",
        "CREATE INDEX IF NOT EXISTS idx_memory_entities_memory "
        "ON memory_entities(memory_id)",
        "CREATE INDEX IF NOT EXISTS idx_memory_entities_entity "
        "ON memory_entities(entity_id)",
        "CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type)",
        "CREATE INDEX IF NOT EXISTS idx_entities_tier ON entities(tier)",
        "CREATE INDEX IF NOT EXISTS idx_entity_aliases_alias ON entity_aliases(alias)",
        "CREATE INDEX IF NOT EXISTS idx_facts_entity ON facts(entity_id)",
        "CREATE INDEX IF NOT EXISTS idx_facts_key ON facts(claim_key)",
        "CREATE INDEX IF NOT EXISTS idx_facts_current "
        "ON facts(entity_id, claim_key) WHERE superseded_at IS NULL",
    )
    for statement in indexes:
        conn.execute(statement)

    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS entities_cleanup_relations
        AFTER DELETE ON entities BEGIN
            DELETE FROM edges
            WHERE source_id = OLD.id OR target_id = OLD.id;
            DELETE FROM memory_entities WHERE entity_id = OLD.id;
            DELETE FROM entity_aliases WHERE entity_id = OLD.id;
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS entities_cleanup_facts
        AFTER DELETE ON entities BEGIN
            DELETE FROM facts WHERE entity_id = OLD.id;
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS memories_cleanup_entities
        AFTER DELETE ON memories BEGIN
            DELETE FROM memory_entities WHERE memory_id = OLD.id;
        END
        """
    )
    _require_columns(
        conn,
        "entities",
        {"id", "name", "type", "aliases", "created_at", "updated_at"},
    )
    _require_columns(
        conn,
        "facts",
        {"id", "entity_id", "claim_key", "claim_value", "observed_at"},
    )


def _migration_production_reliability(conn: sqlite3.Connection) -> None:
    _add_columns(
        conn,
        "memories",
        {
            "project": "TEXT DEFAULT ''",
            "agent": "TEXT DEFAULT ''",
            "access_count": "INTEGER NOT NULL DEFAULT 0",
            "embedding_model": "TEXT DEFAULT ''",
            "embedding_dimensions": "INTEGER",
            "embedding_content_hash": "TEXT DEFAULT ''",
            "embedding_status": "TEXT NOT NULL DEFAULT 'missing'",
            "embedding_updated_at": "REAL",
            "processing_status": "TEXT NOT NULL DEFAULT 'complete'",
            "processing_error": "TEXT DEFAULT ''",
        },
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(project, agent)"
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memories_embedding_status
        ON memories(embedding_status)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS raw_captures (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT '',
            project TEXT NOT NULL DEFAULT '',
            agent TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            memory_id TEXT,
            error TEXT NOT NULL DEFAULT '',
            received_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ingest_events (
            event_id TEXT PRIMARY KEY,
            memory_id TEXT,
            raw_capture_id TEXT,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 1,
            last_error TEXT DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            processed_at REAL,
            FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE SET NULL,
            FOREIGN KEY (raw_capture_id)
                REFERENCES raw_captures(id) ON DELETE SET NULL
        )
        """
    )
    _add_columns(
        conn,
        "ingest_events",
        {"raw_capture_id": "TEXT"},
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ingest_events_status
        ON ingest_events(status, updated_at)
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS raw_captures_cleanup_events
        AFTER DELETE ON raw_captures BEGIN
            UPDATE ingest_events SET raw_capture_id = NULL
            WHERE raw_capture_id = OLD.id;
        END
        """
    )
    _require_columns(
        conn,
        "memories",
        {
            "project",
            "agent",
            "access_count",
            "embedding_model",
            "embedding_dimensions",
            "embedding_content_hash",
            "embedding_status",
            "embedding_updated_at",
            "processing_status",
            "processing_error",
        },
    )
    _require_columns(
        conn,
        "raw_captures",
        {"id", "content", "status", "memory_id", "received_at", "updated_at"},
    )
    _require_columns(
        conn,
        "ingest_events",
        {"event_id", "status", "attempts", "raw_capture_id", "updated_at"},
    )


def _migration_transactional_outbox(conn: sqlite3.Connection) -> None:
    _add_columns(
        conn,
        "raw_captures",
        {
            "requested_category": "TEXT",
            "requested_tier": "TEXT",
            "gate_decision": "TEXT",
            "gate_confidence": "REAL",
            "gate_backend": "TEXT",
            "gate_fallback_used": "INTEGER",
        },
    )
    _add_columns(conn, "facts", {"derivation_key": "TEXT"})
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_derivation_key
        ON facts(derivation_key) WHERE derivation_key IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS outbox_jobs (
            id TEXT PRIMARY KEY,
            raw_capture_id TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL DEFAULT 'capture.process',
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending', 'processing', 'retry', 'complete', 'dead')),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
            available_at REAL NOT NULL,
            lease_expires_at REAL,
            last_error TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            completed_at REAL,
            FOREIGN KEY (raw_capture_id)
                REFERENCES raw_captures(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_outbox_jobs_due
        ON outbox_jobs(status, available_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_outbox_jobs_lease
        ON outbox_jobs(status, lease_expires_at)
        """
    )
    conn.execute(
        """
        INSERT INTO outbox_jobs (
            id, raw_capture_id, event_type, status, attempts,
            available_at, created_at, updated_at
        )
        SELECT
            'outbox_' || id, id, 'capture.process', 'pending', 0,
            received_at, received_at, ?
        FROM raw_captures
        WHERE status = 'pending'
        ON CONFLICT(raw_capture_id) DO NOTHING
        """,
        (time.time(),),
    )
    _require_columns(
        conn,
        "raw_captures",
        {
            "requested_category",
            "requested_tier",
            "gate_decision",
            "gate_confidence",
            "gate_backend",
            "gate_fallback_used",
        },
    )
    _require_columns(conn, "facts", {"derivation_key"})
    _require_columns(
        conn,
        "outbox_jobs",
        {
            "id",
            "raw_capture_id",
            "event_type",
            "status",
            "attempts",
            "available_at",
            "lease_expires_at",
            "last_error",
            "created_at",
            "updated_at",
            "completed_at",
        },
    )


def _migration_task_session_continuity(conn: sqlite3.Connection) -> None:
    """Add provider-neutral scope, task, and append-only session continuity."""
    _add_columns(
        conn,
        "memories",
        {
            "user_id": "TEXT",
            "workspace_id": "TEXT",
            "project_id": "TEXT",
            "repository_id": "TEXT",
            "task_id": "TEXT",
            "session_id": "TEXT",
        },
    )
    _add_columns(
        conn,
        "raw_captures",
        {
            "user_id": "TEXT",
            "workspace_id": "TEXT",
            "project_id": "TEXT",
            "repository_id": "TEXT",
            "task_id": "TEXT",
            "session_id": "TEXT",
        },
    )
    _execute_statements(
        conn,
        """
        CREATE TABLE IF NOT EXISTS agents (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            system_type TEXT NOT NULL DEFAULT 'other',
            trust_level TEXT NOT NULL DEFAULT 'standard',
            write_policy TEXT NOT NULL DEFAULT 'standard',
            created_at REAL NOT NULL,
            last_seen_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS workspaces (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(id, user_id),
            UNIQUE(user_id, name)
        );

        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK(status IN ('active', 'archived')),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(id, user_id, workspace_id),
            UNIQUE(user_id, workspace_id, name),
            FOREIGN KEY (workspace_id, user_id)
                REFERENCES workspaces(id, user_id)
        );

        CREATE TABLE IF NOT EXISTS repositories (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            canonical_path TEXT NOT NULL,
            remote_url TEXT,
            default_branch TEXT,
            access_mode TEXT NOT NULL DEFAULT 'read_only'
                CHECK(access_mode IN ('read_only')),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(id, user_id, workspace_id, project_id),
            UNIQUE(user_id, canonical_path),
            FOREIGN KEY (project_id, user_id, workspace_id)
                REFERENCES projects(id, user_id, workspace_id)
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            repository_id TEXT NOT NULL,
            title TEXT NOT NULL,
            objective TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'planned'
                CHECK(status IN (
                    'planned', 'active', 'blocked', 'review',
                    'completed', 'cancelled'
                )),
            created_by TEXT NOT NULL,
            idempotency_key TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            completed_at REAL,
            UNIQUE(id, user_id, workspace_id, project_id, repository_id),
            FOREIGN KEY (
                repository_id, user_id, workspace_id, project_id
            ) REFERENCES repositories(id, user_id, workspace_id, project_id),
            FOREIGN KEY (created_by) REFERENCES agents(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_idempotency
        ON tasks(user_id, idempotency_key)
        WHERE idempotency_key IS NOT NULL;

        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            initiating_agent_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK(status IN ('active', 'closed')),
            started_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            closed_at REAL,
            summary TEXT NOT NULL DEFAULT '',
            version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
            next_event_sequence INTEGER NOT NULL DEFAULT 1
                CHECK(next_event_sequence >= 1),
            idempotency_key TEXT,
            UNIQUE(id, task_id),
            FOREIGN KEY (task_id) REFERENCES tasks(id),
            FOREIGN KEY (initiating_agent_id) REFERENCES agents(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_idempotency
        ON sessions(task_id, idempotency_key)
        WHERE idempotency_key IS NOT NULL;

        CREATE TABLE IF NOT EXISTS session_participants (
            session_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'observer'
                CHECK(role IN (
                    'owner', 'orchestrator', 'implementer',
                    'reviewer', 'observer'
                )),
            joined_at REAL NOT NULL,
            last_seen_at REAL NOT NULL,
            PRIMARY KEY (session_id, agent_id),
            FOREIGN KEY (session_id) REFERENCES sessions(id),
            FOREIGN KEY (agent_id) REFERENCES agents(id)
        );

        CREATE TABLE IF NOT EXISTS session_events (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            sequence INTEGER NOT NULL CHECK(sequence >= 1),
            event_type TEXT NOT NULL CHECK(event_type IN (
                'session.started', 'session.closed', 'agent.joined',
                'agent.left', 'task.updated', 'checkpoint.created',
                'decision.proposed', 'decision.approved',
                'blocker.reported', 'work.completed',
                'validation.requested'
            )),
            payload_json TEXT NOT NULL DEFAULT '{}',
            idempotency_key TEXT,
            created_at REAL NOT NULL,
            UNIQUE(session_id, sequence),
            FOREIGN KEY (session_id, task_id) REFERENCES sessions(id, task_id),
            FOREIGN KEY (agent_id) REFERENCES agents(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_session_events_idempotency
        ON session_events(session_id, idempotency_key)
        WHERE idempotency_key IS NOT NULL;

        CREATE TABLE IF NOT EXISTS session_checkpoints (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(version >= 1),
            objective TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            completed_json TEXT NOT NULL DEFAULT '[]',
            remaining_json TEXT NOT NULL DEFAULT '[]',
            constraints_json TEXT NOT NULL DEFAULT '[]',
            approved_decisions_json TEXT NOT NULL DEFAULT '[]',
            proposed_decisions_json TEXT NOT NULL DEFAULT '[]',
            open_questions_json TEXT NOT NULL DEFAULT '[]',
            blockers_json TEXT NOT NULL DEFAULT '[]',
            important_files_json TEXT NOT NULL DEFAULT '[]',
            known_failures_json TEXT NOT NULL DEFAULT '[]',
            repository_json TEXT NOT NULL DEFAULT '{}',
            source_agent_id TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            source_sequence INTEGER NOT NULL CHECK(source_sequence >= 1),
            generated INTEGER NOT NULL DEFAULT 0 CHECK(generated IN (0, 1)),
            created_at REAL NOT NULL,
            UNIQUE(session_id, version),
            FOREIGN KEY (session_id) REFERENCES sessions(id),
            FOREIGN KEY (source_agent_id) REFERENCES agents(id),
            FOREIGN KEY (source_event_id) REFERENCES session_events(id)
        );

        CREATE INDEX IF NOT EXISTS idx_tasks_scope_status
        ON tasks(user_id, project_id, repository_id, status);
        CREATE INDEX IF NOT EXISTS idx_sessions_task_status
        ON sessions(task_id, status);
        CREATE INDEX IF NOT EXISTS idx_participants_agent_session
        ON session_participants(agent_id, session_id);
        CREATE INDEX IF NOT EXISTS idx_events_session_created
        ON session_events(session_id, created_at, sequence);
        CREATE INDEX IF NOT EXISTS idx_events_task_created
        ON session_events(task_id, created_at, sequence);
        CREATE INDEX IF NOT EXISTS idx_checkpoints_session_version
        ON session_checkpoints(session_id, version);
        CREATE INDEX IF NOT EXISTS idx_repositories_project
        ON repositories(project_id);
        CREATE INDEX IF NOT EXISTS idx_memories_formal_scope
        ON memories(user_id, workspace_id, project_id, repository_id, task_id);

        CREATE TRIGGER IF NOT EXISTS session_events_no_update
        BEFORE UPDATE ON session_events
        BEGIN
            SELECT RAISE(ABORT, 'session events are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS session_events_no_delete
        BEFORE DELETE ON session_events
        BEGIN
            SELECT RAISE(ABORT, 'session events are append-only');
        END;
        """,
    )
    for table, columns in {
        "agents": {"id", "name", "system_type", "last_seen_at"},
        "workspaces": {"id", "user_id", "name"},
        "projects": {"id", "user_id", "workspace_id", "name", "status"},
        "repositories": {
            "id",
            "user_id",
            "workspace_id",
            "project_id",
            "canonical_path",
            "access_mode",
        },
        "tasks": {
            "id",
            "user_id",
            "workspace_id",
            "project_id",
            "repository_id",
            "status",
        },
        "sessions": {"id", "task_id", "status", "version"},
        "session_participants": {"session_id", "agent_id", "role"},
        "session_events": {
            "id",
            "session_id",
            "task_id",
            "sequence",
            "event_type",
            "payload_json",
        },
        "session_checkpoints": {
            "id",
            "session_id",
            "version",
            "objective",
            "source_event_id",
            "source_sequence",
        },
    }.items():
        _require_columns(conn, table, columns)


def _migration_retrieval_utility(conn: sqlite3.Connection) -> None:
    """Add retrieval telemetry and reversible, explainable utility lifecycle."""
    _add_columns(
        conn,
        "memories",
        {
            "utility_score": "REAL NOT NULL DEFAULT 0.0",
            "utility_policy_version": "TEXT NOT NULL DEFAULT 'utility-v1'",
            "lifecycle_state": "TEXT NOT NULL DEFAULT 'active'",
            "pinned": "INTEGER NOT NULL DEFAULT 0",
            "last_retrieved_at": "REAL",
            "last_selected_at": "REAL",
            "last_injected_at": "REAL",
            "last_expanded_at": "REAL",
            "last_used_at": "REAL",
            "last_successful_use_at": "REAL",
            "retention_review_at": "REAL",
        },
    )
    conn.execute(
        """
        UPDATE memories
        SET lifecycle_state = CASE
            WHEN tier = 'persist' THEN 'stable'
            WHEN tier = 'cold' THEN 'ephemeral'
            ELSE 'active'
        END
        WHERE lifecycle_state = 'active'
        """
    )
    _execute_statements(
        conn,
        """
        CREATE TABLE IF NOT EXISTS retrieval_runs (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            workspace_id TEXT,
            project_id TEXT,
            repository_id TEXT,
            task_id TEXT,
            session_id TEXT,
            agent_id TEXT,
            query TEXT NOT NULL,
            mode TEXT NOT NULL,
            requested_limit INTEGER NOT NULL CHECK(requested_limit > 0),
            created_at REAL NOT NULL,
            latency_ms REAL NOT NULL CHECK(latency_ms >= 0),
            idempotency_key TEXT,
            FOREIGN KEY (task_id) REFERENCES tasks(id),
            FOREIGN KEY (session_id) REFERENCES sessions(id),
            FOREIGN KEY (agent_id) REFERENCES agents(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_retrieval_runs_idempotency
        ON retrieval_runs(agent_id, idempotency_key)
        WHERE idempotency_key IS NOT NULL;

        CREATE TABLE IF NOT EXISTS retrieval_results (
            retrieval_run_id TEXT NOT NULL,
            memory_id TEXT NOT NULL,
            rank INTEGER NOT NULL CHECK(rank >= 1),
            shadow_rank INTEGER CHECK(shadow_rank >= 1),
            keyword_score REAL,
            vector_score REAL,
            graph_score REAL,
            tier_boost REAL,
            utility_score REAL NOT NULL,
            final_score REAL NOT NULL,
            shadow_score REAL NOT NULL,
            selected INTEGER NOT NULL DEFAULT 0 CHECK(selected IN (0, 1)),
            injected INTEGER NOT NULL DEFAULT 0 CHECK(injected IN (0, 1)),
            used INTEGER NOT NULL DEFAULT 0 CHECK(used IN (0, 1)),
            scoring_policy_version TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (retrieval_run_id, memory_id),
            UNIQUE(retrieval_run_id, rank),
            FOREIGN KEY (retrieval_run_id) REFERENCES retrieval_runs(id)
                ON DELETE CASCADE,
            FOREIGN KEY (memory_id) REFERENCES memories(id)
        );

        CREATE TABLE IF NOT EXISTS context_packs (
            id TEXT PRIMARY KEY,
            retrieval_run_id TEXT NOT NULL UNIQUE,
            task_id TEXT,
            session_id TEXT,
            agent_id TEXT,
            token_budget INTEGER,
            estimated_tokens INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            FOREIGN KEY (retrieval_run_id) REFERENCES retrieval_runs(id),
            FOREIGN KEY (task_id) REFERENCES tasks(id),
            FOREIGN KEY (session_id) REFERENCES sessions(id),
            FOREIGN KEY (agent_id) REFERENCES agents(id)
        );

        CREATE TABLE IF NOT EXISTS context_usage (
            id TEXT PRIMARY KEY,
            context_pack_id TEXT NOT NULL,
            memory_id TEXT NOT NULL,
            agent_id TEXT,
            usage_type TEXT NOT NULL CHECK(usage_type IN (
                'returned', 'selected', 'injected', 'expanded', 'used',
                'ignored', 'corrected', 'rejected'
            )),
            created_at REAL NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            idempotency_key TEXT,
            FOREIGN KEY (context_pack_id) REFERENCES context_packs(id),
            FOREIGN KEY (memory_id) REFERENCES memories(id),
            FOREIGN KEY (agent_id) REFERENCES agents(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_context_usage_idempotency
        ON context_usage(context_pack_id, idempotency_key)
        WHERE idempotency_key IS NOT NULL;

        CREATE TABLE IF NOT EXISTS task_outcomes (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            session_id TEXT,
            agent_id TEXT,
            status TEXT NOT NULL,
            successful INTEGER NOT NULL CHECK(successful IN (0, 1)),
            user_correction_count INTEGER NOT NULL DEFAULT 0
                CHECK(user_correction_count >= 0),
            rework_required INTEGER NOT NULL DEFAULT 0
                CHECK(rework_required IN (0, 1)),
            completed_at REAL NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            idempotency_key TEXT,
            FOREIGN KEY (task_id) REFERENCES tasks(id),
            FOREIGN KEY (session_id) REFERENCES sessions(id),
            FOREIGN KEY (agent_id) REFERENCES agents(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_task_outcomes_idempotency
        ON task_outcomes(task_id, idempotency_key)
        WHERE idempotency_key IS NOT NULL;

        CREATE TABLE IF NOT EXISTS utility_history (
            id TEXT PRIMARY KEY,
            memory_id TEXT NOT NULL,
            previous_score REAL NOT NULL,
            new_score REAL NOT NULL,
            reason TEXT NOT NULL,
            components_json TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            created_at REAL NOT NULL,
            FOREIGN KEY (memory_id) REFERENCES memories(id)
        );

        CREATE TABLE IF NOT EXISTS memory_lifecycle_audit (
            id TEXT PRIMARY KEY,
            memory_id TEXT NOT NULL,
            previous_state TEXT NOT NULL,
            new_state TEXT NOT NULL,
            reason TEXT NOT NULL,
            actor_id TEXT,
            policy_version TEXT NOT NULL,
            created_at REAL NOT NULL,
            FOREIGN KEY (memory_id) REFERENCES memories(id),
            FOREIGN KEY (actor_id) REFERENCES agents(id)
        );

        CREATE INDEX IF NOT EXISTS idx_retrieval_runs_scope_created
        ON retrieval_runs(user_id, project_id, repository_id, task_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_retrieval_results_memory
        ON retrieval_results(memory_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_context_usage_memory_type
        ON context_usage(memory_id, usage_type, created_at);
        CREATE INDEX IF NOT EXISTS idx_task_outcomes_task
        ON task_outcomes(task_id, completed_at);
        CREATE INDEX IF NOT EXISTS idx_utility_history_memory
        ON utility_history(memory_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_lifecycle_audit_memory
        ON memory_lifecycle_audit(memory_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_memories_lifecycle_utility
        ON memories(lifecycle_state, pinned, utility_score);
        CREATE INDEX IF NOT EXISTS idx_memories_retention_review
        ON memories(retention_review_at)
        WHERE retention_review_at IS NOT NULL;
        """,
    )
    _require_columns(
        conn,
        "memories",
        {
            "utility_score",
            "utility_policy_version",
            "lifecycle_state",
            "pinned",
            "last_retrieved_at",
            "last_selected_at",
            "last_injected_at",
            "last_expanded_at",
            "last_used_at",
            "last_successful_use_at",
            "retention_review_at",
        },
    )
    for table, columns in {
        "retrieval_runs": {"id", "query", "mode", "latency_ms"},
        "retrieval_results": {
            "retrieval_run_id",
            "memory_id",
            "rank",
            "utility_score",
            "final_score",
        },
        "context_packs": {"id", "retrieval_run_id", "estimated_tokens"},
        "context_usage": {"id", "context_pack_id", "memory_id", "usage_type"},
        "task_outcomes": {
            "id",
            "task_id",
            "agent_id",
            "successful",
            "rework_required",
        },
        "utility_history": {
            "id",
            "memory_id",
            "previous_score",
            "new_score",
            "components_json",
        },
        "memory_lifecycle_audit": {
            "id",
            "memory_id",
            "previous_state",
            "new_state",
        },
    }.items():
        _require_columns(conn, table, columns)


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "core_memory", _migration_core_memory),
    Migration(2, "memory_v2", _migration_memory_v2),
    Migration(3, "graph_and_facts", _migration_graph_and_facts),
    Migration(4, "production_reliability", _migration_production_reliability),
    Migration(5, "transactional_outbox", _migration_transactional_outbox),
    Migration(6, "task_session_continuity", _migration_task_session_continuity),
    Migration(7, "retrieval_utility", _migration_retrieval_utility),
)
CURRENT_SCHEMA_VERSION = MIGRATIONS[-1].version


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(db_path),
        timeout=5.0,
        isolation_level=None,
    )
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc).lower():
            conn.close()
            raise
        logger.debug("Another connection is configuring SQLite journal mode")
    return conn


def _ensure_ledger(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at REAL NOT NULL
            )
            """
        )
        _require_columns(conn, "schema_migrations", {"version", "name", "applied_at"})
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _applied_rows(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    return [
        (int(row[0]), str(row[1]))
        for row in conn.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
    ]


def _validate_history(
    applied: Sequence[tuple[int, str]], migrations: Sequence[Migration]
) -> None:
    expected = [(migration.version, migration.name) for migration in migrations]
    if list(applied) != expected[: len(applied)]:
        actual = ", ".join(f"{version}:{name}" for version, name in applied) or "none"
        raise SchemaMigrationError(
            "unsupported or non-contiguous schema migration history: " + actual
        )


def run_migrations(
    db_path: Path, migrations: Sequence[Migration] = MIGRATIONS
) -> MigrationResult:
    """Bring ``db_path`` to the latest supported schema."""
    ordered = tuple(migrations)
    if not ordered:
        raise SchemaMigrationError("at least one migration is required")
    expected_versions = list(range(1, len(ordered) + 1))
    actual_versions = [migration.version for migration in ordered]
    if actual_versions != expected_versions:
        raise SchemaMigrationError("migration definitions must be contiguous from 1")

    conn = _connect(db_path)
    applied_now: list[AppliedMigration] = []
    try:
        _ensure_ledger(conn)
        initial_rows = _applied_rows(conn)
        _validate_history(initial_rows, ordered)
        from_version = initial_rows[-1][0] if initial_rows else 0

        for migration in ordered:
            conn.execute("BEGIN IMMEDIATE")
            try:
                current_rows = _applied_rows(conn)
                _validate_history(current_rows, ordered)
                current_versions = {version for version, _name in current_rows}
                if migration.version in current_versions:
                    conn.execute("COMMIT")
                    continue

                expected_next = len(current_rows) + 1
                if migration.version != expected_next:
                    raise SchemaMigrationError(
                        f"migration {migration.version}:{migration.name} "
                        f"cannot follow schema version {len(current_rows)}"
                    )

                migration.apply(conn)
                conn.execute(
                    """
                    INSERT INTO schema_migrations (version, name, applied_at)
                    VALUES (?, ?, ?)
                    """,
                    (migration.version, migration.name, time.time()),
                )
                conn.execute("COMMIT")
                applied_now.append(AppliedMigration(migration.version, migration.name))
                logger.info(
                    "Applied schema migration %s:%s",
                    migration.version,
                    migration.name,
                )
            except Exception as exc:
                conn.execute("ROLLBACK")
                if isinstance(exc, SchemaMigrationError):
                    raise
                raise SchemaMigrationError(
                    f"schema migration {migration.version}:{migration.name} failed"
                ) from exc

        final_rows = _applied_rows(conn)
        _validate_history(final_rows, ordered)
        return MigrationResult(
            database=db_path,
            from_version=from_version,
            to_version=final_rows[-1][0] if final_rows else 0,
            applied=tuple(applied_now),
        )
    finally:
        conn.close()
