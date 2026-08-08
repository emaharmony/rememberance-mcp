"""Behavioral coverage for the legacy Recall/Remembrance store importer.

These tests build small synthetic "legacy" SQLite databases by hand,
reproducing the real-world quirks the forensics on the production store
turned up (an invalid/non-contiguous migration history, both an old
``embedding_dim`` and a newer ``embedding_dimensions`` column coexisting,
``owner_id``/``scope`` columns with no current-schema equivalent, and a
``memory_chunks`` table missing ``embedding_dimensions``) — never the real
597 MB production database.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

import pytest

from recall_mcp.legacy_import import (
    LegacyImportError,
    import_legacy_store,
    open_legacy_source,
)
from recall_mcp.store.migrations import run_migrations

# The real production store's actual (invalid) recorded history: versions 6
# and 7 exist under names that do not match any current migration, so
# run_migrations() on the legacy file itself would raise SchemaMigrationError.
# The importer must not care.
INVALID_LEGACY_HISTORY = (
    (1, "core_memory"),
    (2, "memory_v2"),
    (3, "graph_and_facts"),
    (4, "production_reliability"),
    (5, "transactional_outbox"),
    (6, "capture_idempotency"),
    (7, "structured_capture_errors"),
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_legacy_db(
    path: Path,
    *,
    memories: list[dict] = (),
    raw_captures: list[dict] = (),
    memory_chunks: list[dict] = (),
    history: tuple = INVALID_LEGACY_HISTORY,
    include_memory_chunks_table: bool = True,
    chunk_has_embedding_dimensions: bool = False,
) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            """
            CREATE TABLE memories (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                summary TEXT,
                category TEXT DEFAULT 'project',
                tier TEXT DEFAULT 'active',
                key_topics TEXT,
                source TEXT,
                embedding BLOB,
                created_at REAL NOT NULL,
                accessed_at REAL,
                expires_at REAL,
                compiled_truth TEXT,
                timeline TEXT,
                dream_count INTEGER,
                last_dream_at REAL,
                embedding_dim INTEGER,
                embedding_model TEXT,
                owner_id TEXT,
                scope TEXT,
                project TEXT,
                agent TEXT,
                access_count INTEGER,
                embedding_dimensions INTEGER,
                embedding_content_hash TEXT,
                embedding_status TEXT,
                embedding_updated_at REAL,
                processing_status TEXT,
                processing_error TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE raw_captures (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                source TEXT,
                project TEXT,
                agent TEXT,
                status TEXT,
                memory_id TEXT,
                error TEXT,
                error_json TEXT,
                received_at REAL NOT NULL,
                updated_at REAL,
                requested_category TEXT,
                requested_tier TEXT,
                gate_decision TEXT,
                gate_confidence REAL,
                gate_backend TEXT,
                gate_fallback_used INTEGER
            )
            """
        )
        if include_memory_chunks_table:
            dim_column = (
                ", embedding_dimensions INTEGER"
                if chunk_has_embedding_dimensions
                else ""
            )
            conn.execute(
                f"""
                CREATE TABLE memory_chunks (
                    chunk_id TEXT PRIMARY KEY,
                    memory_id TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    embedding BLOB,
                    embedding_dim INTEGER{dim_column},
                    embedding_model TEXT,
                    owner_id TEXT,
                    scope TEXT,
                    created_at REAL
                )
                """
            )
        conn.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at REAL NOT NULL
            )
            """
        )
        now = time.time()
        conn.executemany(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            [(version, name, now) for version, name in history],
        )

        for row in memories:
            columns = list(row.keys())
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(
                f"INSERT INTO memories ({', '.join(columns)}) VALUES ({placeholders})",
                [row[c] for c in columns],
            )
        for row in raw_captures:
            columns = list(row.keys())
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(
                f"INSERT INTO raw_captures ({', '.join(columns)}) VALUES ({placeholders})",
                [row[c] for c in columns],
            )
        for row in memory_chunks:
            columns = list(row.keys())
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(
                f"INSERT INTO memory_chunks ({', '.join(columns)}) VALUES ({placeholders})",
                [row[c] for c in columns],
            )
        conn.commit()
    finally:
        conn.close()


def _memory_row(
    mem_id: str,
    content: str,
    created_at: float,
    *,
    accessed_at: float | None = None,
    expires_at: float | None = None,
    summary: str = "",
    category: str = "project",
    tier: str = "active",
    source: str = "capture",
    owner_id: str | None = "legacy-owner",
    scope: str | None = "legacy-scope",
    embedding: bytes | None = b"\x00" * 8,
    embedding_dim: int | None = 8,
    embedding_model: str | None = "hash-256",
) -> dict:
    return {
        "id": mem_id,
        "content": content,
        "summary": summary,
        "category": category,
        "tier": tier,
        "source": source,
        "created_at": created_at,
        "accessed_at": accessed_at if accessed_at is not None else created_at,
        "expires_at": expires_at,
        "owner_id": owner_id,
        "scope": scope,
        "embedding": embedding,
        "embedding_dim": embedding_dim,
        "embedding_model": embedding_model,
    }


def _target_db(tmp_path: Path) -> Path:
    return tmp_path / "target" / "memory.db"


def _target_memory_rows(target_db: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(target_db))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM memories").fetchall()
    finally:
        conn.close()


def _target_memory_ids(target_db: Path) -> set[str]:
    return {row["id"] for row in _target_memory_rows(target_db)}


# ---------------------------------------------------------------------------
# Read-only source guard
# ---------------------------------------------------------------------------


def test_open_legacy_source_rejects_missing_file(tmp_path):
    with pytest.raises(LegacyImportError, match="not found"):
        open_legacy_source(tmp_path / "does-not-exist.db")


def test_open_legacy_source_rejects_non_recall_database(tmp_path):
    path = tmp_path / "unrelated.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE unrelated (id TEXT)")
    conn.commit()
    conn.close()

    with pytest.raises(LegacyImportError, match="memories"):
        open_legacy_source(path)


def test_read_only_connection_refuses_writes(tmp_path):
    path = tmp_path / "legacy.db"
    _build_legacy_db(path, memories=[_memory_row("mem_1", "hello", 1000.0)])

    source = open_legacy_source(path)
    try:
        with pytest.raises(LegacyImportError):
            source.execute("DELETE FROM memories")
        with pytest.raises(LegacyImportError):
            source.execute(
                "INSERT INTO memories (id, content, created_at) VALUES ('x','y',1)"
            )
        with pytest.raises(LegacyImportError):
            source.execute("PRAGMA journal_mode=DELETE")
        # A plain read still works.
        rows = source.execute("SELECT id FROM memories").fetchall()
        assert rows == [("mem_1",)]
    finally:
        source.close()


def test_source_file_itself_is_never_modified(tmp_path):
    path = tmp_path / "legacy.db"
    _build_legacy_db(
        path,
        memories=[_memory_row("mem_1", "hello world", 1000.0)],
    )
    before = _sha256(path)

    import_legacy_store(path, _target_db(tmp_path), dry_run=False)

    after = _sha256(path)
    assert before == after


# ---------------------------------------------------------------------------
# Invalid migration history is not a blocker
# ---------------------------------------------------------------------------


def test_invalid_source_migration_history_does_not_block_import(tmp_path):
    path = tmp_path / "legacy.db"
    _build_legacy_db(
        path,
        memories=[_memory_row("mem_1", "content one", 1000.0)],
        history=INVALID_LEGACY_HISTORY,
    )

    # Sanity: run_migrations against this file directly would refuse it.
    from recall_mcp.store.migrations import SchemaMigrationError

    with pytest.raises(SchemaMigrationError):
        run_migrations(path)

    report = import_legacy_store(path, _target_db(tmp_path), dry_run=False)
    assert report["would_import"] == 1
    assert _target_memory_ids(_target_db(tmp_path)) == {"mem_1"}


# ---------------------------------------------------------------------------
# Round trip + idempotency
# ---------------------------------------------------------------------------


def test_round_trip_preserves_id_created_at_and_content(tmp_path):
    path = tmp_path / "legacy.db"
    created_at = 1700000000.0
    _build_legacy_db(
        path,
        memories=[
            _memory_row(
                "mem_abc",
                "the quick brown fox",
                created_at,
                category="architecture",
                tier="persist",
                source="claude-code",
                summary="a summary",
            )
        ],
    )
    target_db = _target_db(tmp_path)

    report = import_legacy_store(path, target_db, dry_run=False)
    assert report["would_import"] == 1

    rows = _target_memory_rows(target_db)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "mem_abc"
    assert row["content"] == "the quick brown fox"
    assert row["created_at"] == created_at
    assert row["category"] == "architecture"
    assert row["tier"] == "persist"
    assert row["source"] == "claude-code"
    assert row["summary"] == "a summary"


def test_second_run_is_idempotent(tmp_path):
    path = tmp_path / "legacy.db"
    _build_legacy_db(
        path,
        memories=[
            _memory_row("mem_1", "alpha", 1000.0),
            _memory_row("mem_2", "beta", 1001.0),
        ],
    )
    target_db = _target_db(tmp_path)

    first = import_legacy_store(path, target_db, dry_run=False)
    assert first["would_import"] == 2
    assert first["already_present"] == 0

    second = import_legacy_store(path, target_db, dry_run=False)
    assert second["would_import"] == 0
    assert second["already_present"] == 2

    assert len(_target_memory_rows(target_db)) == 2


# ---------------------------------------------------------------------------
# Idempotency regression: dedupe state must survive repeated runs
#
# Reproduces a real bug found against production data: a "leader" row (the
# first copy of some duplicate content, the one that actually gets
# imported) and its "follower" duplicate(s) (correctly skipped as exact/
# near dupes on the first run) must stay correctly classified no matter how
# many times the importer is re-run against the same target -- even after
# the leader itself becomes "already present" and so short-circuits before
# ever repeating its own classification work.
# ---------------------------------------------------------------------------


def test_exact_dupe_follower_stays_suppressed_across_repeated_runs(tmp_path):
    path = tmp_path / "legacy.db"
    _build_legacy_db(
        path,
        memories=[
            _memory_row("mem_leader", "duplicated text", 1000.0),
            _memory_row("mem_follower", "duplicated text", 2000.0),
        ],
    )
    target_db = _target_db(tmp_path)

    first = import_legacy_store(path, target_db, dry_run=False)
    assert first["would_import"] == 1
    assert first["skipped_exact_dupe"] == 1
    assert _target_memory_ids(target_db) == {"mem_leader"}

    # Second run: the leader is now already-present. Before the fix, its
    # hash was never recomputed on this pass (the already-present check
    # short-circuited before hashing), so the follower stopped looking like
    # a duplicate of anything and was imported for real -- this is exactly
    # the bug that showed up against production data.
    second = import_legacy_store(path, target_db, dry_run=False)
    assert second["would_import"] == 0
    assert second["already_present"] == 1
    assert second["skipped_exact_dupe"] == 1
    assert _target_memory_ids(target_db) == {"mem_leader"}

    third = import_legacy_store(path, target_db, dry_run=False)
    assert third["would_import"] == 0
    assert third["already_present"] == 1
    assert third["skipped_exact_dupe"] == 1
    assert len(_target_memory_rows(target_db)) == 1


def test_near_dupe_classification_stays_accurate_across_repeated_runs(tmp_path):
    path = tmp_path / "legacy.db"
    shared_prefix = "x" * 150
    _build_legacy_db(
        path,
        memories=[
            _memory_row("mem_early", shared_prefix + " one", 1000.0),
            _memory_row("mem_late", shared_prefix + " two", 2000.0),
        ],
    )
    target_db = _target_db(tmp_path)

    first = import_legacy_store(path, target_db, dry_run=False)
    assert first["would_import"] == 2
    assert first["near_dupe_detected"] == 1

    second = import_legacy_store(path, target_db, dry_run=False)
    assert second["would_import"] == 0
    assert second["already_present"] == 2
    assert len(_target_memory_rows(target_db)) == 2

    third = import_legacy_store(path, target_db, dry_run=False)
    assert third["would_import"] == 0
    assert third["already_present"] == 2
    assert len(_target_memory_rows(target_db)) == 2


def test_import_three_times_converges_to_zero_and_rows_are_unchanged(tmp_path):
    """The case that actually failed in production: repeated real runs must
    stop finding new rows to import, and the already-imported rows must
    stay byte-for-byte identical -- not re-inserted, not duplicated."""
    path = tmp_path / "legacy.db"
    _build_legacy_db(
        path,
        memories=[
            _memory_row("mem_1", "alpha", 1000.0),
            _memory_row("mem_2", "alpha", 1001.0),  # exact dupe of mem_1
            _memory_row("mem_3", "beta", 1002.0),
        ],
    )
    target_db = _target_db(tmp_path)

    import_legacy_store(path, target_db, dry_run=False)
    snapshot_after_first = [dict(r) for r in _target_memory_rows(target_db)]

    second = import_legacy_store(path, target_db, dry_run=False)
    assert second["would_import"] == 0

    third = import_legacy_store(path, target_db, dry_run=False)
    assert third["would_import"] == 0

    snapshot_after_third = [dict(r) for r in _target_memory_rows(target_db)]
    assert snapshot_after_first == snapshot_after_third
    assert _target_memory_ids(target_db) == {"mem_1", "mem_3"}


def test_dupe_leader_pre_populated_in_target_suppresses_follower(tmp_path):
    """Simulates a target populated by something other than a prior full run
    of this importer (a partial earlier run, a hand-inserted row, a
    different source) -- the dedupe state must be seeded from the target's
    real content, not merely carried over in-process from a previous
    invocation of this function."""
    path = tmp_path / "legacy.db"
    _build_legacy_db(
        path,
        memories=[
            _memory_row("mem_leader", "duplicated text", 1000.0),
            _memory_row("mem_follower", "duplicated text", 2000.0),
        ],
    )
    target_db = _target_db(tmp_path)

    # Pre-populate the target directly with the leader, bypassing the
    # importer entirely.
    run_migrations(target_db)
    conn = sqlite3.connect(str(target_db))
    conn.execute(
        "INSERT INTO memories (id, content, category, tier, created_at, accessed_at) "
        "VALUES ('mem_leader', 'duplicated text', 'project', 'active', 1000.0, 1000.0)"
    )
    conn.commit()
    conn.close()

    report = import_legacy_store(path, target_db, dry_run=False)
    assert report["already_present"] == 1  # mem_leader
    assert report["skipped_exact_dupe"] == 1  # mem_follower must NOT import
    assert report["would_import"] == 0
    assert _target_memory_ids(target_db) == {"mem_leader"}


def test_salvage_and_raw_captures_stay_zero_on_third_run(tmp_path):
    path = tmp_path / "legacy.db"
    _build_legacy_db(
        path,
        memories=[_memory_row("mem_1", "hello", 1000.0)],
        raw_captures=[
            {
                "id": "cap_1",
                "content": "raw capture text",
                "received_at": 999.0,
                "memory_id": "mem_1",
            }
        ],
        memory_chunks=[
            {
                "chunk_id": "mem_orphan::chunk::0",
                "memory_id": "mem_orphan",
                "chunk_index": 0,
                "content": "orphan content",
                "created_at": 1000.0,
            }
        ],
    )
    target_db = _target_db(tmp_path)

    import_legacy_store(path, target_db, dry_run=False)
    second = import_legacy_store(path, target_db, dry_run=False)
    assert second["salvaged"] == 0
    assert second["salvaged_already_present"] == 1
    assert second["raw_captures"]["would_import"] == 0
    assert second["raw_captures"]["already_present"] == 1

    third = import_legacy_store(path, target_db, dry_run=False)
    assert third["salvaged"] == 0
    assert third["salvaged_already_present"] == 1
    assert third["raw_captures"]["would_import"] == 0
    assert third["raw_captures"]["already_present"] == 1
    assert len(_target_memory_rows(target_db)) == 2  # mem_1 + salvaged mem_orphan


# ---------------------------------------------------------------------------
# Dedupe: exact
# ---------------------------------------------------------------------------


def test_exact_duplicate_content_keeps_earliest_by_default(tmp_path):
    path = tmp_path / "legacy.db"
    _build_legacy_db(
        path,
        memories=[
            _memory_row("mem_early", "duplicated text", 1000.0),
            _memory_row("mem_late", "duplicated text", 2000.0),
        ],
    )
    target_db = _target_db(tmp_path)

    report = import_legacy_store(path, target_db, dry_run=False)
    assert report["would_import"] == 1
    assert report["skipped_exact_dupe"] == 1
    assert _target_memory_ids(target_db) == {"mem_early"}


def test_no_dedupe_exact_keeps_both_copies(tmp_path):
    path = tmp_path / "legacy.db"
    _build_legacy_db(
        path,
        memories=[
            _memory_row("mem_early", "duplicated text", 1000.0),
            _memory_row("mem_late", "duplicated text", 2000.0),
        ],
    )
    target_db = _target_db(tmp_path)

    report = import_legacy_store(path, target_db, dry_run=False, dedupe_exact=False)
    assert report["would_import"] == 2
    assert report["skipped_exact_dupe"] == 0
    assert _target_memory_ids(target_db) == {"mem_early", "mem_late"}


# ---------------------------------------------------------------------------
# Dedupe: near
# ---------------------------------------------------------------------------


def test_near_duplicates_reported_but_kept_by_default(tmp_path):
    path = tmp_path / "legacy.db"
    shared_prefix = "x" * 150  # > 120 chars, identical prefix, divergent tail
    _build_legacy_db(
        path,
        memories=[
            _memory_row("mem_early", shared_prefix + " one", 1000.0),
            _memory_row("mem_late", shared_prefix + " two", 2000.0),
        ],
    )
    target_db = _target_db(tmp_path)

    report = import_legacy_store(path, target_db, dry_run=False)
    assert report["near_dupe_detected"] == 1
    assert report["skipped_near_dupe"] == 0
    assert report["would_import"] == 2
    assert _target_memory_ids(target_db) == {"mem_early", "mem_late"}


def test_dedupe_near_flag_drops_the_later_near_duplicate(tmp_path):
    path = tmp_path / "legacy.db"
    shared_prefix = "x" * 150
    _build_legacy_db(
        path,
        memories=[
            _memory_row("mem_early", shared_prefix + " one", 1000.0),
            _memory_row("mem_late", shared_prefix + " two", 2000.0),
        ],
    )
    target_db = _target_db(tmp_path)

    report = import_legacy_store(path, target_db, dry_run=False, dedupe_near=True)
    assert report["near_dupe_detected"] == 1
    assert report["skipped_near_dupe"] == 1
    assert report["would_import"] == 1
    assert _target_memory_ids(target_db) == {"mem_early"}


# ---------------------------------------------------------------------------
# Expiry filtering
# ---------------------------------------------------------------------------


def test_expired_memories_are_skipped_by_default(tmp_path):
    path = tmp_path / "legacy.db"
    past = time.time() - 86400
    _build_legacy_db(
        path,
        memories=[
            _memory_row("mem_expired", "old news", 1000.0, expires_at=past),
            _memory_row("mem_alive", "still relevant", 1001.0),
        ],
    )
    target_db = _target_db(tmp_path)

    report = import_legacy_store(path, target_db, dry_run=False)
    assert report["skipped_expired"] == 1
    assert report["would_import"] == 1
    assert _target_memory_ids(target_db) == {"mem_alive"}


def test_include_expired_flag_imports_them(tmp_path):
    path = tmp_path / "legacy.db"
    past = time.time() - 86400
    _build_legacy_db(
        path,
        memories=[
            _memory_row("mem_expired", "old news", 1000.0, expires_at=past),
            _memory_row("mem_alive", "still relevant", 1001.0),
        ],
    )
    target_db = _target_db(tmp_path)

    report = import_legacy_store(path, target_db, dry_run=False, skip_expired=False)
    assert report["skipped_expired"] == 0
    assert report["would_import"] == 2
    assert _target_memory_ids(target_db) == {"mem_expired", "mem_alive"}


# ---------------------------------------------------------------------------
# Orphaned chunk salvage
# ---------------------------------------------------------------------------


def test_orphan_chunk_salvage_reassembles_in_chunk_index_order(tmp_path):
    path = tmp_path / "legacy.db"
    _build_legacy_db(
        path,
        memories=[_memory_row("mem_survivor", "still here", 1000.0)],
        memory_chunks=[
            # Orphaned: "mem_purged" has no row in memories.
            {
                "chunk_id": "mem_purged::chunk::1",
                "memory_id": "mem_purged",
                "chunk_index": 1,
                "content": "second part.",
                "created_at": 1500.0,
            },
            {
                "chunk_id": "mem_purged::chunk::0",
                "memory_id": "mem_purged",
                "chunk_index": 0,
                "content": "first part.",
                "created_at": 1400.0,
            },
            # Not orphaned: parent memory exists.
            {
                "chunk_id": "mem_survivor::chunk::0",
                "memory_id": "mem_survivor",
                "chunk_index": 0,
                "content": "still here",
                "created_at": 1000.0,
            },
        ],
        chunk_has_embedding_dimensions=False,
    )
    target_db = _target_db(tmp_path)

    report = import_legacy_store(path, target_db, dry_run=False)
    assert report["salvaged"] == 1
    assert report["salvage_orphaned_memory_id_count"] == 1

    rows = {row["id"]: row for row in _target_memory_rows(target_db)}
    assert "mem_purged" in rows
    salvaged = rows["mem_purged"]
    assert salvaged["content"] == "first part.\n\nsecond part."
    assert salvaged["source"] == "legacy-salvage"
    assert "legacy-salvage" in salvaged["summary"].lower()
    assert salvaged["created_at"] == 1400.0

    # The non-orphaned chunk must not spawn a memory of its own.
    assert "mem_survivor::chunk::0" not in rows


def test_orphan_chunk_salvage_can_be_disabled(tmp_path):
    path = tmp_path / "legacy.db"
    _build_legacy_db(
        path,
        memory_chunks=[
            {
                "chunk_id": "mem_purged::chunk::0",
                "memory_id": "mem_purged",
                "chunk_index": 0,
                "content": "orphaned content",
                "created_at": 1000.0,
            }
        ],
    )
    target_db = _target_db(tmp_path)

    report = import_legacy_store(
        path, target_db, dry_run=False, salvage_orphan_chunks=False
    )
    assert report["salvaged"] == 0
    assert "mem_purged" not in _target_memory_ids(target_db)


def test_memory_chunks_missing_embedding_dimensions_does_not_break_import(tmp_path):
    """Real-world shape: the production memory_chunks table never grew an
    embedding_dimensions column, unlike the current schema's memory_chunks."""
    path = tmp_path / "legacy.db"
    _build_legacy_db(
        path,
        memories=[_memory_row("mem_1", "hello", 1000.0)],
        memory_chunks=[
            {
                "chunk_id": "mem_orphan::chunk::0",
                "memory_id": "mem_orphan",
                "chunk_index": 0,
                "content": "orphan content",
                "created_at": 1000.0,
            }
        ],
        chunk_has_embedding_dimensions=False,
    )
    target_db = _target_db(tmp_path)

    report = import_legacy_store(path, target_db, dry_run=False)
    assert report["would_import"] == 1
    assert report["salvaged"] == 1


def test_salvage_idempotent_on_second_run(tmp_path):
    path = tmp_path / "legacy.db"
    _build_legacy_db(
        path,
        memory_chunks=[
            {
                "chunk_id": "mem_purged::chunk::0",
                "memory_id": "mem_purged",
                "chunk_index": 0,
                "content": "orphaned content",
                "created_at": 1000.0,
            }
        ],
    )
    target_db = _target_db(tmp_path)

    first = import_legacy_store(path, target_db, dry_run=False)
    assert first["salvaged"] == 1

    second = import_legacy_store(path, target_db, dry_run=False)
    assert second["salvaged"] == 0
    assert second["salvaged_already_present"] == 1
    assert len(_target_memory_rows(target_db)) == 1


# ---------------------------------------------------------------------------
# Legacy-only columns are dropped
# ---------------------------------------------------------------------------


def test_legacy_embeddings_and_owner_scope_are_not_carried_across(tmp_path):
    path = tmp_path / "legacy.db"
    _build_legacy_db(
        path,
        memories=[
            _memory_row(
                "mem_1",
                "hello",
                1000.0,
                owner_id="some-owner",
                scope="some-scope",
                embedding=b"\xff" * 32,
                embedding_dim=32,
                embedding_model="hash-256",
            )
        ],
    )
    target_db = _target_db(tmp_path)

    import_legacy_store(path, target_db, dry_run=False)

    target_columns = {
        row[1]
        for row in sqlite3.connect(str(target_db))
        .execute("PRAGMA table_info(memories)")
        .fetchall()
    }
    # The current schema never had owner_id/scope/embedding_dim at all.
    assert "owner_id" not in target_columns
    assert "scope" not in target_columns
    assert "embedding_dim" not in target_columns

    row = _target_memory_rows(target_db)[0]
    assert row["embedding"] is None
    assert row["embedding_status"] == "missing"


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing(tmp_path):
    path = tmp_path / "legacy.db"
    _build_legacy_db(
        path,
        memories=[_memory_row("mem_1", "hello", 1000.0)],
        raw_captures=[
            {
                "id": "cap_1",
                "content": "raw capture text",
                "received_at": 1000.0,
                "memory_id": "mem_1",
            }
        ],
        memory_chunks=[
            {
                "chunk_id": "mem_orphan::chunk::0",
                "memory_id": "mem_orphan",
                "chunk_index": 0,
                "content": "orphan content",
                "created_at": 1000.0,
            }
        ],
    )
    target_db = _target_db(tmp_path)

    report = import_legacy_store(path, target_db, dry_run=True)
    assert report["would_import"] == 1
    assert report["salvaged"] == 1
    assert report["raw_captures"]["would_import"] == 1

    # Nothing was created at all.
    assert not target_db.exists()


def test_dry_run_against_existing_target_does_not_modify_it(tmp_path):
    path = tmp_path / "legacy.db"
    _build_legacy_db(path, memories=[_memory_row("mem_1", "hello", 1000.0)])
    target_db = _target_db(tmp_path)

    # Pre-create and populate the target for real.
    run_migrations(target_db)
    conn = sqlite3.connect(str(target_db))
    conn.execute(
        "INSERT INTO memories (id, content, category, tier, created_at, accessed_at) "
        "VALUES ('existing', 'pre-existing memory', 'project', 'active', 500.0, 500.0)"
    )
    conn.commit()
    conn.close()
    before_bytes = target_db.read_bytes()
    before_mtime = target_db.stat().st_mtime_ns

    report = import_legacy_store(path, target_db, dry_run=True)
    assert report["would_import"] == 1
    assert report["already_present"] == 0

    after_bytes = target_db.read_bytes()
    after_mtime = target_db.stat().st_mtime_ns
    assert before_bytes == after_bytes
    assert before_mtime == after_mtime
    assert _target_memory_ids(target_db) == {"existing"}


# ---------------------------------------------------------------------------
# raw_captures
# ---------------------------------------------------------------------------


def test_raw_captures_imported_with_memory_id_preserved_when_target_exists(tmp_path):
    path = tmp_path / "legacy.db"
    _build_legacy_db(
        path,
        memories=[_memory_row("mem_1", "hello", 1000.0)],
        raw_captures=[
            {
                "id": "cap_1",
                "content": "raw capture text",
                "received_at": 999.0,
                "memory_id": "mem_1",
                "status": "complete",
            }
        ],
    )
    target_db = _target_db(tmp_path)

    report = import_legacy_store(path, target_db, dry_run=False)
    assert report["raw_captures"]["would_import"] == 1

    conn = sqlite3.connect(str(target_db))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM raw_captures WHERE id = 'cap_1'").fetchone()
    conn.close()
    assert row is not None
    assert row["memory_id"] == "mem_1"
    assert row["content"] == "raw capture text"


def test_raw_capture_memory_id_nulled_when_parent_memory_missing(tmp_path):
    path = tmp_path / "legacy.db"
    _build_legacy_db(
        path,
        raw_captures=[
            {
                "id": "cap_1",
                "content": "orphaned provenance",
                "received_at": 999.0,
                "memory_id": "mem_never_imported",
            }
        ],
    )
    target_db = _target_db(tmp_path)

    report = import_legacy_store(path, target_db, dry_run=False)
    assert report["raw_captures"]["would_import"] == 1

    conn = sqlite3.connect(str(target_db))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM raw_captures WHERE id = 'cap_1'").fetchone()
    conn.close()
    assert row["memory_id"] is None


def test_raw_captures_second_run_is_idempotent(tmp_path):
    path = tmp_path / "legacy.db"
    _build_legacy_db(
        path,
        raw_captures=[
            {"id": "cap_1", "content": "text", "received_at": 999.0},
        ],
    )
    target_db = _target_db(tmp_path)

    first = import_legacy_store(path, target_db, dry_run=False)
    assert first["raw_captures"]["would_import"] == 1

    second = import_legacy_store(path, target_db, dry_run=False)
    assert second["raw_captures"]["would_import"] == 0
    assert second["raw_captures"]["already_present"] == 1


def test_missing_raw_captures_table_is_reported_and_skipped(tmp_path):
    path = tmp_path / "legacy.db"
    _build_legacy_db(path, memories=[_memory_row("mem_1", "hello", 1000.0)])
    # Drop raw_captures entirely to simulate a divergent lineage without it.
    conn = sqlite3.connect(str(path))
    conn.execute("DROP TABLE raw_captures")
    conn.commit()
    conn.close()

    target_db = _target_db(tmp_path)
    report = import_legacy_store(path, target_db, dry_run=False)
    assert report["raw_captures"]["would_import"] == 0
    assert report["raw_captures"]["skipped_reason"]


# ---------------------------------------------------------------------------
# Invalid rows are skipped defensively, not fatal
# ---------------------------------------------------------------------------


def test_row_with_empty_content_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "legacy.db"
    _build_legacy_db(
        path,
        memories=[
            _memory_row("mem_good", "fine", 1000.0),
            # The synthetic fixture's memories.content is NOT NULL (matching
            # the real store), so an empty string -- not a NULL -- is the
            # realistic shape of "content effectively missing" for a
            # divergent legacy lineage; the importer treats both as
            # falsy/invalid via the same truthiness check.
            _memory_row("mem_bad", "", 2000.0),
        ],
    )

    target_db = _target_db(tmp_path)
    report = import_legacy_store(path, target_db, dry_run=False)
    assert report["skipped_invalid"] == 1
    assert report["would_import"] == 1
    assert _target_memory_ids(target_db) == {"mem_good"}


# ---------------------------------------------------------------------------
# --limit
# ---------------------------------------------------------------------------


def test_limit_bounds_the_number_of_memory_rows_processed(tmp_path):
    path = tmp_path / "legacy.db"
    _build_legacy_db(
        path,
        memories=[
            _memory_row(f"mem_{i}", f"content {i}", 1000.0 + i) for i in range(5)
        ],
    )
    target_db = _target_db(tmp_path)

    report = import_legacy_store(path, target_db, dry_run=True, limit=2)
    assert report["would_import"] == 2
