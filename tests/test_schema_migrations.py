"""Behavioral coverage for Recall's formal SQLite migrations."""

from __future__ import annotations

import json
import sqlite3
import time
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from recall_mcp.admin import command_migrate
from recall_mcp.config import Settings
from recall_mcp.feedback import RetrievalFeedbackService
from recall_mcp.store import MemoryStore
from recall_mcp.store.migrations import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    Migration,
    SchemaMigrationError,
    run_migrations,
)


def _objects(db_path: Path, kind: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = ?", (kind,)
            ).fetchall()
        }


def _columns(db_path: Path, table: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }


def test_fresh_database_applies_all_migrations(tmp_path):
    db_path = tmp_path / "memory.db"

    result = run_migrations(db_path)

    assert result.from_version == 0
    assert result.to_version == CURRENT_SCHEMA_VERSION
    assert [migration.as_dict() for migration in result.applied] == [
        {"version": migration.version, "name": migration.name}
        for migration in MIGRATIONS
    ]


def test_migration_8_preserves_phase_2_context_pack_and_is_idempotent(tmp_path):
    db_path = tmp_path / "phase-2.db"
    run_migrations(db_path, MIGRATIONS[:7])
    now = time.time()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO retrieval_runs (
                id, query, mode, requested_limit, created_at, latency_ms
            ) VALUES ('run-old', 'query', 'balanced', 10, ?, 1.0)
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT INTO context_packs (
                id, retrieval_run_id, estimated_tokens, created_at
            ) VALUES ('context-old', 'run-old', 12, ?)
            """,
            (now,),
        )

    upgraded = run_migrations(db_path, MIGRATIONS[:8])
    repeated = run_migrations(db_path, MIGRATIONS[:8])

    assert [migration.version for migration in upgraded.applied] == [8]
    assert repeated.from_version == repeated.to_version == 8
    assert {"context_pack_items", "context_pack_references"}.issubset(
        _objects(db_path, "table")
    )
    assert {"pack_json", "source_fingerprint", "schema_version"}.issubset(
        _columns(db_path, "context_packs")
    )
    with sqlite3.connect(db_path) as conn:
        preserved = conn.execute(
            "SELECT schema_version, estimated_tokens FROM context_packs WHERE id = ?",
            ("context-old",),
        ).fetchone()
    assert preserved == (1, 12)
    assert {
        "schema_migrations",
        "memories",
        "dream_log",
        "entities",
        "edges",
        "memory_entities",
        "entity_aliases",
        "facts",
        "raw_captures",
        "ingest_events",
        "outbox_jobs",
    }.issubset(_objects(db_path, "table"))
    assert {
        "project",
        "agent",
        "compiled_truth",
        "embedding_status",
        "processing_status",
    }.issubset(_columns(db_path, "memories"))
    assert {
        "requested_category",
        "requested_tier",
        "gate_decision",
    }.issubset(_columns(db_path, "raw_captures"))
    assert "derivation_key" in _columns(db_path, "facts")
    assert {
        "entities_cleanup_relations",
        "entities_cleanup_facts",
        "memories_cleanup_entities",
        "raw_captures_cleanup_events",
    }.issubset(_objects(db_path, "trigger"))

    with sqlite3.connect(db_path) as conn:
        history = conn.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
        memory_entity_parents = {
            row[2] for row in conn.execute("PRAGMA foreign_key_list(memory_entities)")
        }
        ingest_event_parents = {
            row[2] for row in conn.execute("PRAGMA foreign_key_list(ingest_events)")
        }
        outbox_parents = {
            row[2] for row in conn.execute("PRAGMA foreign_key_list(outbox_jobs)")
        }
    assert history == [
        (migration.version, migration.name) for migration in MIGRATIONS[:8]
    ]
    assert memory_entity_parents == {"memories", "entities"}
    assert ingest_event_parents == {"memories", "raw_captures"}
    assert outbox_parents == {"raw_captures"}


def test_migration_12_adds_memory_chunks_and_is_idempotent(tmp_path):
    db_path = tmp_path / "phase-11.db"
    run_migrations(db_path, MIGRATIONS[:11])
    now = time.time()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO memories (
                id, content, summary, category, tier, key_topics, source,
                created_at, accessed_at
            ) VALUES ('mem-legacy', 'preserve me', 's', 'project', 'active', '[]', 'test', ?, ?)
            """,
            (now, now),
        )

    upgraded = run_migrations(db_path, MIGRATIONS[:12])
    repeated = run_migrations(db_path, MIGRATIONS[:12])

    assert [migration.version for migration in upgraded.applied] == [12]
    assert repeated.from_version == repeated.to_version == 12
    assert repeated.applied == ()
    assert "memory_chunks" in _objects(db_path, "table")
    assert {
        "chunk_id",
        "memory_id",
        "chunk_index",
        "content",
        "embedding",
        "embedding_model",
        "embedding_dimensions",
        "created_at",
    }.issubset(_columns(db_path, "memory_chunks"))

    with sqlite3.connect(db_path) as conn:
        preserved = conn.execute(
            "SELECT content FROM memories WHERE id = 'mem-legacy'"
        ).fetchone()
        assert preserved == ("preserve me",)
        conn.execute(
            """
            INSERT INTO memory_chunks (
                chunk_id, memory_id, chunk_index, content, created_at
            ) VALUES ('mem-legacy::chunk::0', 'mem-legacy', 0, 'preserve me', ?)
            """,
            (now,),
        )
        conn.commit()
        chunk_parents = {
            row[2] for row in conn.execute("PRAGMA foreign_key_list(memory_chunks)")
        }
    assert chunk_parents == {"memories"}

    # Chunks are derived state: removing the parent memory cascades.
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("DELETE FROM memories WHERE id = 'mem-legacy'")
        conn.commit()
        remaining = conn.execute(
            "SELECT COUNT(*) FROM memory_chunks WHERE memory_id = 'mem-legacy'"
        ).fetchone()[0]
    assert remaining == 0


def test_unversioned_legacy_database_preserves_rows(tmp_path):
    db_path = tmp_path / "legacy.db"
    created_at = time.time()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE memories (
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
                expires_at REAL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO memories (
                id, content, summary, category, tier, key_topics, source,
                created_at, accessed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-memory",
                "preserve this canonical row",
                "legacy",
                "project",
                "persist",
                "[]",
                "test",
                created_at,
                created_at,
            ),
        )

    result = run_migrations(db_path)

    assert result.to_version == CURRENT_SCHEMA_VERSION
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT content, tier, project, embedding_status, processing_status
            FROM memories WHERE id = 'legacy-memory'
            """
        ).fetchone()
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert row == (
        "preserve this canonical row",
        "persist",
        "",
        "missing",
        "complete",
    )


def test_partially_migrated_database_resumes_in_order(tmp_path):
    db_path = tmp_path / "partial.db"
    first = run_migrations(db_path, MIGRATIONS[:2])
    assert first.to_version == 2

    resumed = run_migrations(db_path)

    assert resumed.from_version == 2
    assert [migration.version for migration in resumed.applied] == list(
        range(3, CURRENT_SCHEMA_VERSION + 1)
    )
    assert resumed.to_version == CURRENT_SCHEMA_VERSION


def test_migrations_are_idempotent(tmp_path):
    db_path = tmp_path / "memory.db"
    run_migrations(db_path)

    repeated = run_migrations(db_path)

    assert repeated.from_version == CURRENT_SCHEMA_VERSION
    assert repeated.to_version == CURRENT_SCHEMA_VERSION
    assert repeated.applied == ()


def test_concurrent_migration_attempts_serialize(tmp_path):
    db_path = tmp_path / "concurrent.db"
    barrier = Barrier(4)

    def migrate():
        barrier.wait()
        return run_migrations(db_path)

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _index: migrate(), range(4)))

    assert all(result.to_version == CURRENT_SCHEMA_VERSION for result in results)
    with sqlite3.connect(db_path) as conn:
        history = conn.execute(
            "SELECT version, COUNT(*) FROM schema_migrations GROUP BY version"
        ).fetchall()
    assert history == [(migration.version, 1) for migration in MIGRATIONS]


def test_failed_migration_rolls_back_schema_and_ledger(tmp_path):
    db_path = tmp_path / "failed.db"

    def fail_after_ddl(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE must_roll_back (id INTEGER PRIMARY KEY)")
        raise RuntimeError("forced migration failure")

    with pytest.raises(SchemaMigrationError, match="1:forced_failure"):
        run_migrations(db_path, (Migration(1, "forced_failure", fail_after_ddl),))

    assert "must_roll_back" not in _objects(db_path, "table")
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT * FROM schema_migrations").fetchall() == []


@pytest.mark.parametrize(
    "history",
    [
        [(2, "memory_v2")],
        [(1, "wrong_name")],
        [(999, "future_schema")],
    ],
)
def test_unsupported_or_gapped_history_fails_closed(tmp_path, history):
    db_path = tmp_path / "unsupported.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at REAL NOT NULL
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO schema_migrations (version, name, applied_at)
            VALUES (?, ?, ?)
            """,
            [(version, name, time.time()) for version, name in history],
        )

    with pytest.raises(SchemaMigrationError, match="unsupported|non-contiguous"):
        run_migrations(db_path)

    assert "memories" not in _objects(db_path, "table")


def test_admin_migrate_reports_versions_and_integrity(tmp_path, capsys):
    settings = Settings(
        BASE_DIR=tmp_path,
        DB_PATH=tmp_path / "memory.db",
        EMBEDDINGS_ENABLED=False,
    )

    assert command_migrate(settings, Namespace()) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["from_version"] == 0
    assert first["to_version"] == CURRENT_SCHEMA_VERSION
    assert len(first["applied"]) == CURRENT_SCHEMA_VERSION

    assert command_migrate(settings, Namespace()) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["from_version"] == CURRENT_SCHEMA_VERSION
    assert repeated["applied"] == []

    report = MemoryStore(settings.DB_PATH).integrity_report()
    assert report["schema_version"] == CURRENT_SCHEMA_VERSION
    assert report["schema_current"] is True
    assert report["ok"] is True


def _fk_on_delete(db_path: Path, table: str) -> dict[str, str]:
    """Map referenced-table -> ON DELETE action for a table's foreign keys."""
    with sqlite3.connect(db_path) as conn:
        return {
            str(row[2]): str(row[6])
            for row in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        }


def test_migration_13_adds_memory_fk_cascades_and_is_idempotent(tmp_path):
    db_path = tmp_path / "phase-12.db"
    run_migrations(db_path, MIGRATIONS[:12])
    now = time.time()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO memories (
                id, content, summary, category, tier, key_topics, source,
                created_at, accessed_at
            ) VALUES ('mem-legacy', 'preserve me', 's', 'project', 'active', '[]', 'test', ?, ?)
            """,
            (now, now),
        )
        conn.commit()

    upgraded = run_migrations(db_path, MIGRATIONS[:13])
    repeated = run_migrations(db_path, MIGRATIONS[:13])

    assert [migration.version for migration in upgraded.applied] == [13]
    assert repeated.from_version == repeated.to_version == 13
    assert repeated.applied == ()

    with sqlite3.connect(db_path) as conn:
        preserved = conn.execute(
            "SELECT content FROM memories WHERE id = 'mem-legacy'"
        ).fetchone()
        assert preserved == ("preserve me",)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    assert _fk_on_delete(db_path, "retrieval_results")["memories"] == "CASCADE"
    assert _fk_on_delete(db_path, "context_pack_items")["memories"] == "CASCADE"
    assert _fk_on_delete(db_path, "context_usage")["memories"] == "CASCADE"
    assert _fk_on_delete(db_path, "context_pack_references")["memories"] == "SET NULL"
    assert _fk_on_delete(db_path, "utility_history")["memories"] == "SET NULL"
    assert _fk_on_delete(db_path, "memory_lifecycle_audit")["memories"] == "SET NULL"
    # Unaffected sibling cascades on the same tables must survive the rebuild.
    assert _fk_on_delete(db_path, "retrieval_results")["retrieval_runs"] == "CASCADE"
    assert _fk_on_delete(db_path, "context_pack_items")["context_packs"] == "CASCADE"


def test_migration_13_purges_preexisting_orphans_seeded_at_v12(tmp_path):
    db_path = tmp_path / "orphans.db"
    run_migrations(db_path, MIGRATIONS[:12])
    now = time.time()

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO memories (
                id, content, summary, category, tier, key_topics, source,
                created_at, accessed_at
            ) VALUES ('mem-live', 'live', 's', 'project', 'active', '[]', 't', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO retrieval_runs (
                id, query, mode, requested_limit, created_at, latency_ms
            ) VALUES ('run-1', 'q', 'balanced', 10, ?, 1.0)
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT INTO retrieval_results (
                retrieval_run_id, memory_id, rank, utility_score, final_score,
                shadow_score, scoring_policy_version, created_at
            ) VALUES ('run-1', 'mem-live', 1, 0.5, 0.5, 0.5, 'v1', ?)
            """,
            (now,),
        )
        # Orphans: rows left behind by the pre-migration-13 broken delete path,
        # referencing memories that no longer exist.
        conn.execute(
            """
            INSERT INTO retrieval_results (
                retrieval_run_id, memory_id, rank, utility_score, final_score,
                shadow_score, scoring_policy_version, created_at
            ) VALUES ('run-1', 'mem-gone-1', 2, 0.5, 0.5, 0.5, 'v1', ?)
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT INTO context_packs (
                id, retrieval_run_id, estimated_tokens, created_at
            ) VALUES ('pack-1', 'run-1', 10, ?)
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT INTO context_pack_items (
                context_pack_id, memory_id, disposition, position, estimated_tokens
            ) VALUES ('pack-1', 'mem-gone-2', 'inline', 1, 5)
            """
        )
        conn.execute(
            """
            INSERT INTO context_pack_references (
                id, context_pack_id, memory_id, type, title, summary,
                estimated_tokens, created_at
            ) VALUES ('ref-1', 'pack-1', 'mem-gone-3', 'memory', 't', 's', 5, ?)
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT INTO context_usage (
                id, context_pack_id, memory_id, usage_type, created_at
            ) VALUES ('use-1', 'pack-1', 'mem-gone-4', 'used', ?)
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT INTO utility_history (
                id, memory_id, previous_score, new_score, reason,
                components_json, policy_version, created_at
            ) VALUES ('uh-1', 'mem-gone-5', 0.1, 0.2, 'r', '{}', 'v1', ?)
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT INTO memory_lifecycle_audit (
                id, memory_id, previous_state, new_state, reason,
                policy_version, created_at
            ) VALUES ('mla-1', 'mem-gone-6', 'active', 'cold', 'r', 'v1', ?)
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT INTO memory_chunks (
                chunk_id, memory_id, chunk_index, content, created_at
            ) VALUES ('chunk-gone', 'mem-gone-7', 0, 'c', ?)
            """,
            (now,),
        )
        conn.execute(
            "INSERT INTO memory_entities (memory_id, entity_id) "
            "VALUES ('mem-gone-8', 'ent-gone')"
        )
        conn.execute(
            """
            INSERT INTO raw_captures (id, content, memory_id, received_at, updated_at)
            VALUES ('rc-1', 'content', 'mem-gone-9', ?, ?)
            """,
            (now, now),
        )
        conn.commit()

    run_migrations(db_path, MIGRATIONS[:13])

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

        # Rows describing a specific memory (CASCADE targets) are gone entirely.
        assert conn.execute(
            "SELECT memory_id FROM retrieval_results ORDER BY memory_id"
        ).fetchall() == [("mem-live",)]
        assert (
            conn.execute("SELECT COUNT(*) FROM context_pack_items").fetchone()[0] == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM context_usage").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memory_chunks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memory_entities").fetchone()[0] == 0

        # Audit/reference rows survive with the dangling pointer nulled out.
        assert conn.execute(
            "SELECT id, memory_id FROM context_pack_references"
        ).fetchall() == [("ref-1", None)]
        assert conn.execute("SELECT id, memory_id FROM utility_history").fetchall() == [
            ("uh-1", None)
        ]
        assert conn.execute(
            "SELECT id, memory_id FROM memory_lifecycle_audit"
        ).fetchall() == [("mla-1", None)]
        assert conn.execute("SELECT id, memory_id FROM raw_captures").fetchall() == [
            ("rc-1", None)
        ]

        # The live memory's rows are untouched by the orphan purge.
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM memories WHERE id = 'mem-live'"
            ).fetchone()[0]
            == 1
        )


def test_delete_memory_after_retrieval_cascades_dependents(tmp_path):
    """Regression test for the original bug: a memory returned by a search
    could never be deleted because retrieval_results.memory_id had no
    ON DELETE clause. After migration 13, delete() must succeed and the
    dependent retrieval_results row must be gone.
    """
    settings = Settings(
        BASE_DIR=tmp_path,
        DB_PATH=tmp_path / "memory.db",
        EMBEDDINGS_ENABLED=False,
    )
    store = MemoryStore(settings.DB_PATH)
    feedback = RetrievalFeedbackService(settings.DB_PATH, settings)

    mem_id = store.store(
        content="the answer is 42",
        summary="the answer",
        category="project",
        tier="active",
        key_topics=[],
    )

    run_id = feedback.record_retrieval_run(
        query="what is the answer",
        mode="balanced",
        requested_limit=5,
        latency_ms=12.0,
        results=[{"id": mem_id, "score": 0.9, "keyword_score": 0.9}],
    )

    with sqlite3.connect(settings.DB_PATH) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM retrieval_results WHERE memory_id = ?",
                (mem_id,),
            ).fetchone()[0]
            == 1
        )

    assert store.delete(mem_id) is True

    with sqlite3.connect(settings.DB_PATH) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM retrieval_results WHERE memory_id = ?",
                (mem_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM retrieval_runs WHERE id = ?", (run_id,)
            ).fetchone()[0]
            == 1
        )
