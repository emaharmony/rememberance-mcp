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


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "core_memory", _migration_core_memory),
    Migration(2, "memory_v2", _migration_memory_v2),
    Migration(3, "graph_and_facts", _migration_graph_and_facts),
    Migration(4, "production_reliability", _migration_production_reliability),
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
