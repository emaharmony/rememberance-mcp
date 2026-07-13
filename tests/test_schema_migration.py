"""Phase 2 — provider-agnostic + multi-user forward-compat schema columns.

Verifies the additive `_migrate_v2` migration adds the new columns to `memories`
without losing existing data, applies correct defaults, and is idempotent.
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from remembrance_mcp.store.memory import MemoryStoreV2
from remembrance_mcp.store.store import MemoryStore

NEW_COLUMNS = {"embedding_dim", "embedding_model", "owner_id", "scope"}


@pytest.fixture()
def db_path():
    d = tempfile.mkdtemp(prefix="remembrance-schema-")
    yield Path(d) / "memory.db"


def _columns(db: Path) -> dict[str, dict]:
    with sqlite3.connect(str(db)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("PRAGMA table_info(memories)").fetchall()
    return {r["name"]: dict(r) for r in rows}


def test_new_columns_present_after_migration(db_path):
    store = MemoryStore(db_path=db_path)
    MemoryStoreV2(v1_store=store)  # runs _migrate_v2
    cols = _columns(db_path)
    assert NEW_COLUMNS <= set(cols), f"missing: {NEW_COLUMNS - set(cols)}"
    # embedding (v1) column is reused, not duplicated
    assert "embedding" in cols


def test_column_types(db_path):
    MemoryStoreV2(v1_store=MemoryStore(db_path=db_path))
    cols = _columns(db_path)
    assert cols["embedding_dim"]["type"].upper() == "INTEGER"
    assert cols["embedding_model"]["type"].upper() == "TEXT"
    assert cols["owner_id"]["type"].upper() == "TEXT"
    assert cols["scope"]["type"].upper() == "TEXT"


def test_existing_row_survives_with_defaults(db_path):
    # Insert a V1 memory BEFORE the V2 migration, then migrate.
    store = MemoryStore(db_path=db_path)
    mem_id = store.store(
        content="We decided the event bus is canonical.",
        summary="event bus decision",
        category="decision",
        tier="persist",
        key_topics=["event-bus"],
        source="test",
    )
    MemoryStoreV2(v1_store=store)  # additive migration on a populated table

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, content, embedding_dim, embedding_model, owner_id, scope "
            "FROM memories WHERE id = ?",
            (mem_id,),
        ).fetchone()

    assert row is not None, "existing row lost during migration"
    assert row["content"] == "We decided the event bus is canonical."
    # Provider-agnostic embedding columns: empty until Phase 3 embeds the row.
    assert row["embedding_dim"] is None
    assert row["embedding_model"] == ""
    # Multi-user forward-compat: nullable owner, private-by-default scope.
    assert row["owner_id"] is None
    assert row["scope"] == "private"


def test_no_data_loss(db_path):
    store = MemoryStore(db_path=db_path)
    for i in range(5):
        store.store(
            content=f"memory {i}",
            summary=f"s{i}",
            category="project",
            tier="active",
            key_topics=[],
            source="test",
        )
    MemoryStoreV2(v1_store=store)
    with sqlite3.connect(str(db_path)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    assert count == 5


def test_migration_idempotent(db_path):
    store = MemoryStore(db_path=db_path)
    MemoryStoreV2(v1_store=store)
    # Running the migration again must not raise (columns already exist).
    MemoryStoreV2(v1_store=store)
    cols = _columns(db_path)
    assert NEW_COLUMNS <= set(cols)


def test_scope_default_applies_to_new_inserts(db_path):
    # A row inserted through the V1 store API (which doesn't set scope) still gets
    # the 'private' default from the column definition after migration.
    store = MemoryStore(db_path=db_path)
    MemoryStoreV2(v1_store=store)
    mem_id = store.store(
        content="post-migration insert",
        summary="s",
        category="project",
        tier="active",
        key_topics=[],
        source="test",
    )
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT scope, embedding_model, owner_id, embedding_dim FROM memories WHERE id = ?",
            (mem_id,),
        ).fetchone()
    assert row["scope"] == "private"
    assert row["embedding_model"] == ""
    assert row["owner_id"] is None
    assert row["embedding_dim"] is None
