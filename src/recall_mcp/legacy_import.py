"""Legacy Recall/Remembrance store export & re-ingest.

Some legacy stores accumulated three divergent schema lineages (see
``docs/migrating-to-recall.md``) and cannot be brought to the current head
schema in place via ``recall-admin migrate`` — their recorded migration
history does not match any prefix of ``MIGRATIONS`` in
``recall_mcp.store.migrations``, so ``run_migrations`` correctly refuses to
touch them.

This module reads what is salvageable straight out of the raw tables of
such a store — without ever running its migration history — and writes a
clean, current-schema copy of it into a target Recall home:

- ``memories``: the real content, preserving ``id``/``created_at`` so a
  second run of the same import is a no-op (report-only, no new rows).
- ``raw_captures``: supplementary provenance, where the current schema can
  accept it cleanly.
- Orphaned ``memory_chunks`` (rows whose ``memory_id`` no longer has a
  parent row in ``memories`` — typically because a dream-purge cycle failed
  to cascade the delete): these are the *only* surviving copy of that
  content, so they are reassembled by ``chunk_index`` into a new memory
  under the original ``memory_id``, clearly marked as salvaged.

Legacy embeddings (always a non-semantic ``hash-256`` placeholder — see the
forensics referenced in ``docs/migrating-to-recall.md``) and the legacy
``owner_id``/``scope`` columns (superseded by the current
``user_id``/``workspace_id``/``project_id``/``repository_id`` scope model,
which has no equivalent to map from) are intentionally dropped, never
carried across.

Nothing here ever writes to the source database. ``open_legacy_source``
opens it with the SQLite ``mode=ro`` URI flag and ``PRAGMA query_only=ON``
(two independent engine-level guards), and every statement executed
against it additionally passes through :class:`ReadOnlySourceConnection`,
which allowlists only read statements before handing them to SQLite. That
third, application-level guard means a coding mistake here fails loudly
with :class:`LegacyImportError` instead of silently depending on the other
two layers alone.
"""

from __future__ import annotations

import hashlib
import itertools
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

# How often to emit a progress line while streaming a long-running phase.
# Progress goes to stderr so it never mixes with the JSON report on stdout.
_PROGRESS_INTERVAL = 200


def _progress(message: str) -> None:
    print(f"[import-legacy] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LegacyImportError(Exception):
    """Raised when a legacy source cannot be imported, or a write to it is attempted."""


# ---------------------------------------------------------------------------
# Read-only source guard
# ---------------------------------------------------------------------------

# Every statement this module sends to the source is written by this module,
# never assembled from source data, so an allowlist of exact prefixes is
# sufficient and much safer than trying to deny-list write keywords.
_ALLOWED_SOURCE_STATEMENT_PREFIXES: tuple[str, ...] = (
    "select",
    "pragma table_info(",
)


class ReadOnlySourceConnection:
    """Wraps a sqlite3 connection so it can only ever run read statements.

    This is a third, application-level layer on top of the ``mode=ro`` URI
    flag and ``PRAGMA query_only=ON`` already applied to the underlying
    connection by :func:`open_legacy_source` — belt, suspenders, and a
    third belt.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    @staticmethod
    def _check(sql: str) -> None:
        normalized = " ".join(sql.split()).strip().lower()
        if not normalized.startswith(_ALLOWED_SOURCE_STATEMENT_PREFIXES):
            raise LegacyImportError(
                "refusing to run a non-read statement against the read-only "
                f"legacy source: {sql[:120]!r}"
            )

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        self._check(sql)
        return self._connection.execute(sql, parameters)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "ReadOnlySourceConnection":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def open_legacy_source(path: Path) -> ReadOnlySourceConnection:
    """Open ``path`` strictly read-only and confirm it looks like a legacy store.

    Raises :class:`LegacyImportError` if the file does not exist, cannot be
    opened, or does not have a ``memories`` table with the minimum columns
    every schema lineage shares. Deliberately does **not** check the
    source's migration history — that history may be invalid, and that is
    exactly the situation this tool exists to recover from.
    """
    if not path.exists():
        raise LegacyImportError(f"legacy source database not found: {path}")
    uri = f"file:{path.as_posix()}?mode=ro"
    try:
        raw = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError as exc:
        raise LegacyImportError(f"cannot open legacy source read-only: {exc}") from exc
    try:
        raw.execute("PRAGMA query_only = ON")
    except sqlite3.DatabaseError as exc:
        raw.close()
        raise LegacyImportError(
            f"source does not look like a SQLite database: {exc}"
        ) from exc
    connection = ReadOnlySourceConnection(raw)
    _validate_legacy_shape(connection)
    return connection


def _validate_legacy_shape(connection: ReadOnlySourceConnection) -> None:
    columns = _table_columns(connection, "memories")
    if not columns:
        raise LegacyImportError(
            "source has no 'memories' table; does not look like a "
            "Recall/Remembrance store"
        )
    required = {"id", "content", "created_at"}
    missing = required - columns
    if missing:
        raise LegacyImportError(
            f"source 'memories' table is missing required columns: {sorted(missing)}"
        )


def _table_columns(connection: ReadOnlySourceConnection, table: str) -> set[str]:
    try:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.DatabaseError:
        return set()
    return {str(row[1]) for row in rows}


# ---------------------------------------------------------------------------
# Column mapping (documented, not silent)
# ---------------------------------------------------------------------------

# Legacy column name -> current schema column name. Identity today, but kept
# explicit and separate from "carried across at all" so a future rename is a
# one-line diff instead of a silent behavior change.
MEMORY_COLUMNS: tuple[str, ...] = (
    "id",
    "content",
    "summary",
    "category",
    "tier",
    "key_topics",
    "source",
    "created_at",
    "accessed_at",
    "expires_at",
    "compiled_truth",
    "timeline",
    "dream_count",
    "last_dream_at",
    "project",
    "agent",
    "access_count",
    "processing_status",
    "processing_error",
)

# Columns intentionally dropped, and why:
#   embedding, embedding_dim, embedding_dimensions, embedding_model,
#   embedding_content_hash, embedding_status, embedding_updated_at
#       -- legacy embeddings are a documented "hash-256" SHA-256 scaffold
#          with NO semantic meaning; carrying them across would poison
#          vector search. Left unset so the normal embed path repopulates
#          them (new rows default embedding_status='missing').
#   owner_id, scope
#       -- superseded by user_id/workspace_id/project_id/repository_id/
#          task_id/session_id, which has no equivalent to map from.
DROPPED_MEMORY_COLUMNS: tuple[str, ...] = (
    "embedding",
    "embedding_dim",
    "embedding_dimensions",
    "embedding_model",
    "embedding_content_hash",
    "embedding_status",
    "embedding_updated_at",
    "owner_id",
    "scope",
)

RAW_CAPTURE_COLUMNS: tuple[str, ...] = (
    "id",
    "content",
    "source",
    "project",
    "agent",
    "status",
    "memory_id",
    "error",
    "received_at",
    "updated_at",
    "requested_category",
    "requested_tier",
    "gate_decision",
    "gate_confidence",
    "gate_backend",
    "gate_fallback_used",
)

# error_json: legacy-only, no current equivalent column; dropped.
DROPPED_RAW_CAPTURE_COLUMNS: tuple[str, ...] = ("error_json",)

_SALVAGE_SOURCE = "legacy-salvage"


# ---------------------------------------------------------------------------
# Target connections
# ---------------------------------------------------------------------------


def _existing_ids(db_path: Path, table: str) -> set[str]:
    """Read existing ids from the target without creating or migrating it.

    Used for both dry-run (where nothing else touches the target either)
    and the pre-insert idempotency check of a real run (called *after* the
    target has already been migrated to head).
    """
    if not db_path.exists():
        return set()
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return set()
    try:
        try:
            rows = conn.execute(f"SELECT id FROM {table}").fetchall()
        except sqlite3.OperationalError:
            return set()
        return {str(row[0]) for row in rows}
    finally:
        conn.close()


def _classify_and_register(
    content: str,
    seen_hashes: dict[str, str],
    seen_near_keys: dict[str, str],
    owner_id: str,
) -> tuple[bool, bool]:
    """Classify ``content`` against the running dedupe state, then register it.

    Registration happens whenever ``content`` is not itself an exact
    duplicate, *regardless* of what the caller ends up doing with the
    classification (importing it, skipping it as already-present, whatever
    else). That is the crux of making a later duplicate of *this* content
    still recognized as a duplicate on a repeat run: the old code only
    registered a hash when the row went on to be imported, so once a
    "leader" row became already-present (and so was short-circuited before
    ever reaching this point), its hash silently stopped existing for the
    rest of that run and its "follower" duplicates -- correctly skipped in
    a prior run -- came back as if new.

    Returns ``(is_exact_dupe, is_near_dupe)``.
    """
    content_hash = hashlib.sha256(
        content.encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    is_exact_dupe = content_hash in seen_hashes
    is_near_dupe = False
    if not is_exact_dupe:
        seen_hashes[content_hash] = owner_id
        near_key = content[:120]
        is_near_dupe = near_key in seen_near_keys
        if not is_near_dupe:
            seen_near_keys[near_key] = owner_id
    return is_exact_dupe, is_near_dupe


def _seed_memory_dedupe_state(
    db_path: Path,
) -> tuple[set[str], dict[str, str], dict[str, str]]:
    """Stream the target's *existing* memories once to build both the id
    set and the content-dedupe state (hash + near-key), so the importer's
    notion of "already seen" reflects the target's real content — not just
    whatever happened to run earlier in this same process. This is what
    makes the importer correct against a target populated by some other
    means too (a partial earlier run, a differently-sourced import), not
    only against its own immediately-preceding invocation.

    Streamed row-by-row from a cursor (never ``.fetchall()``): the target
    can hold thousands of multi-KB memory rows and this must not hold them
    all in memory at once just to compute a set of hashes.
    """
    existing_ids: set[str] = set()
    seen_hashes: dict[str, str] = {}
    seen_near_keys: dict[str, str] = {}
    if not db_path.exists():
        return existing_ids, seen_hashes, seen_near_keys
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return existing_ids, seen_hashes, seen_near_keys
    try:
        try:
            cursor = conn.execute(
                "SELECT id, content FROM memories ORDER BY created_at ASC, id ASC"
            )
        except sqlite3.OperationalError:
            return existing_ids, seen_hashes, seen_near_keys
        for row in cursor:
            legacy_id = str(row[0])
            content = row[1]
            existing_ids.add(legacy_id)
            if not content:
                continue
            _classify_and_register(content, seen_hashes, seen_near_keys, legacy_id)
        return existing_ids, seen_hashes, seen_near_keys
    finally:
        conn.close()


def _connect_target_for_write(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path))
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _content_bytes(text: str) -> int:
    return len(text.encode("utf-8", errors="surrogatepass"))


def _iso_date(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return time.strftime("%Y-%m-%d", time.gmtime(epoch))


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def import_legacy_store(
    source_path: Path,
    target_db_path: Path,
    *,
    dry_run: bool = False,
    skip_expired: bool = True,
    dedupe_exact: bool = True,
    dedupe_near: bool = False,
    salvage_orphan_chunks: bool = True,
    limit: int | None = None,
) -> dict[str, Any]:
    """Import memories/raw_captures/orphaned-chunk salvage from a legacy store.

    Returns a JSON-serializable plan/report dict. When ``dry_run`` is True,
    nothing is written anywhere — not to the source (never, regardless of
    ``dry_run``) and not to ``target_db_path`` either; the target is opened
    read-only (or not opened at all, if it does not exist yet) purely to
    determine what is already present.

    Re-running with the same arguments against the same target imports
    zero new rows: every legacy id is checked against the target before
    insertion (``already_present``), so imports are naturally idempotent
    without relying on a bare ``INSERT OR IGNORE`` to explain what happened.
    """
    source = open_legacy_source(source_path)
    try:
        from recall_mcp.store.migrations import run_migrations

        if not dry_run:
            run_migrations(target_db_path)

        existing_memory_ids, seen_hashes, seen_near_keys = _seed_memory_dedupe_state(
            target_db_path
        )
        existing_raw_capture_ids = _existing_ids(target_db_path, "raw_captures")

        target: sqlite3.Connection | None = None
        if not dry_run:
            target = _connect_target_for_write(target_db_path)
            target.execute("BEGIN IMMEDIATE")

        try:
            report = _run_import(
                source,
                target,
                existing_memory_ids,
                existing_raw_capture_ids,
                seen_hashes,
                seen_near_keys,
                dry_run=dry_run,
                skip_expired=skip_expired,
                dedupe_exact=dedupe_exact,
                dedupe_near=dedupe_near,
                salvage_orphan_chunks=salvage_orphan_chunks,
                limit=limit,
            )
            if target is not None:
                target.commit()
            return report
        except Exception:
            if target is not None:
                target.rollback()
            raise
        finally:
            if target is not None:
                target.close()
    finally:
        source.close()


def _run_import(
    source: ReadOnlySourceConnection,
    target: sqlite3.Connection | None,
    existing_memory_ids: set[str],
    existing_raw_capture_ids: set[str],
    seen_hashes: dict[str, str],
    seen_near_keys: dict[str, str],
    *,
    dry_run: bool,
    skip_expired: bool,
    dedupe_exact: bool,
    dedupe_near: bool,
    salvage_orphan_chunks: bool,
    limit: int | None,
) -> dict[str, Any]:
    now = time.time()
    plan: dict[str, Any] = {
        "would_import": 0,
        "would_import_bytes": 0,
        "already_present": 0,
        "skipped_expired": 0,
        "skipped_exact_dupe": 0,
        "near_dupe_detected": 0,
        "skipped_near_dupe": 0,
        "skipped_invalid": 0,
        "salvaged": 0,
        "salvaged_bytes": 0,
        "salvaged_already_present": 0,
        "salvage_orphaned_memory_id_count": 0,
        "raw_captures": {
            "would_import": 0,
            "already_present": 0,
            "skipped_reason": None,
        },
    }
    earliest: float | None = None
    latest: float | None = None

    def _touch_range(created_at: float | None) -> None:
        nonlocal earliest, latest
        if created_at is None:
            return
        earliest = created_at if earliest is None else min(earliest, created_at)
        latest = created_at if latest is None else max(latest, created_at)

    memory_columns = [
        c for c in MEMORY_COLUMNS if c in _table_columns(source, "memories")
    ]
    select_sql = (
        f"SELECT {', '.join(memory_columns)} FROM memories "
        "ORDER BY created_at ASC, id ASC"
    )
    total_memories = source.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    _progress(f"scanning {total_memories} legacy memories (limit={limit})")

    processed = 0

    # Iterate the cursor lazily (do NOT .fetchall()): one row is ~948 KB and
    # there are 1,115 of them, so materializing the whole result set would
    # hold the entire table's content in memory at once.
    for row in source.execute(select_sql):
        if limit is not None and processed >= limit:
            break
        processed += 1
        if processed % _PROGRESS_INTERVAL == 0:
            _progress(f"memories: {processed}/{total_memories} scanned")

        record = dict(zip(memory_columns, row))
        legacy_id = record.get("id")
        content = record.get("content")
        created_at = record.get("created_at")
        if not legacy_id or not content or created_at is None:
            plan["skipped_invalid"] += 1
            continue

        # Classify and register this content BEFORE any short-circuit below
        # (already-present, expired), so a later row with identical/near-
        # identical content is still recognized as a duplicate even though
        # *this* row itself won't reach the would_import path this run.
        is_exact_dupe, is_near_dupe = _classify_and_register(
            content, seen_hashes, seen_near_keys, legacy_id
        )

        if legacy_id in existing_memory_ids:
            plan["already_present"] += 1
            continue

        expires_at = record.get("expires_at")
        if expires_at is not None and expires_at < now and skip_expired:
            plan["skipped_expired"] += 1
            continue

        if is_exact_dupe:
            if dedupe_exact:
                plan["skipped_exact_dupe"] += 1
                continue
        elif is_near_dupe:
            plan["near_dupe_detected"] += 1
            if dedupe_near:
                plan["skipped_near_dupe"] += 1
                continue

        plan["would_import"] += 1
        plan["would_import_bytes"] += _content_bytes(content)
        _touch_range(created_at)
        existing_memory_ids.add(legacy_id)

        if target is not None:
            accessed_at = record.get("accessed_at")
            if accessed_at is None:
                accessed_at = created_at
            _insert_memory(
                target,
                record,
                memory_columns,
                accessed_at=accessed_at,
            )

    _progress(
        f"memories: done — {plan['would_import']} would_import, "
        f"{plan['already_present']} already_present"
    )

    if salvage_orphan_chunks:
        _salvage_orphan_chunks(source, target, existing_memory_ids, plan, _touch_range)
        _progress(
            f"orphan chunk salvage: {plan['salvaged']} recovered "
            f"(of {plan['salvage_orphaned_memory_id_count']} orphaned memory ids)"
        )

    _import_raw_captures(
        source, target, existing_raw_capture_ids, existing_memory_ids, plan
    )
    _progress(
        f"raw_captures: {plan['raw_captures']['would_import']} would_import, "
        f"{plan['raw_captures']['already_present']} already_present"
    )

    plan["date_range"] = {
        "earliest_created_at": earliest,
        "latest_created_at": latest,
        "earliest_date": _iso_date(earliest),
        "latest_date": _iso_date(latest),
    }
    plan["dry_run"] = dry_run
    return plan


def _insert_memory(
    target: sqlite3.Connection,
    record: dict[str, Any],
    columns: list[str],
    *,
    accessed_at: float,
) -> None:
    # Omit any column whose legacy value is None instead of inserting an
    # explicit NULL: several current-schema columns (access_count,
    # dream_count, category, tier, processing_status, ...) are
    # NOT NULL DEFAULT <x>, and a bare legacy NULL there would violate the
    # constraint. Omitting the column lets the target schema's own default
    # apply, which is what we want either way.
    to_insert: dict[str, Any] = {
        c: record[c] for c in columns if record.get(c) is not None
    }
    to_insert["accessed_at"] = accessed_at  # always explicit; never None
    insert_columns = list(to_insert.keys())
    placeholders = ", ".join("?" for _ in insert_columns)
    target.execute(
        f"INSERT INTO memories ({', '.join(insert_columns)}) VALUES ({placeholders})",
        [to_insert[c] for c in insert_columns],
    )


def _salvage_orphan_chunks(
    source: ReadOnlySourceConnection,
    target: sqlite3.Connection | None,
    existing_memory_ids: set[str],
    plan: dict[str, Any],
    touch_range,
) -> None:
    chunk_columns = _table_columns(source, "memory_chunks")
    required = {"memory_id", "chunk_index", "content"}
    if not required.issubset(chunk_columns):
        return

    # Qualified with the mc. alias: memory_chunks and memories both have a
    # created_at column, so an unqualified reference is ambiguous once the
    # LEFT JOIN is in scope.
    select_created_at = "mc.created_at" if "created_at" in chunk_columns else "NULL"
    rows = source.execute(
        f"""
        SELECT mc.memory_id, mc.chunk_index, mc.content, {select_created_at}
        FROM memory_chunks mc
        LEFT JOIN memories m ON m.id = mc.memory_id
        WHERE m.id IS NULL
        ORDER BY mc.memory_id ASC, mc.chunk_index ASC
        """
    ).fetchall()
    plan["salvage_orphaned_memory_id_count"] = len({r[0] for r in rows})

    for memory_id, group_iter in itertools.groupby(rows, key=lambda r: r[0]):
        group = list(group_iter)
        if memory_id in existing_memory_ids:
            plan["salvaged_already_present"] += 1
            continue

        chunks = sorted(group, key=lambda r: r[1])
        combined = "\n\n".join(str(r[2]) for r in chunks if r[2])
        if not combined:
            continue
        chunk_created_ats = [r[3] for r in chunks if r[3] is not None]
        salvaged_created_at = (
            min(chunk_created_ats) if chunk_created_ats else time.time()
        )

        plan["salvaged"] += 1
        plan["salvaged_bytes"] += _content_bytes(combined)
        touch_range(salvaged_created_at)
        existing_memory_ids.add(memory_id)

        if target is not None:
            summary = (
                f"[legacy-salvage] Recovered from {len(chunks)} orphaned "
                "chunk(s) of a memory purged by a dream cycle that failed "
                "to cascade the delete (chunks survived; the parent memory "
                "row did not)."
            )
            target.execute(
                """
                INSERT INTO memories (
                    id, content, summary, source, created_at, accessed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    combined,
                    summary,
                    _SALVAGE_SOURCE,
                    salvaged_created_at,
                    salvaged_created_at,
                ),
            )


def _import_raw_captures(
    source: ReadOnlySourceConnection,
    target: sqlite3.Connection | None,
    existing_raw_capture_ids: set[str],
    existing_memory_ids: set[str],
    plan: dict[str, Any],
) -> None:
    source_columns = _table_columns(source, "raw_captures")
    required = {"id", "content", "received_at"}
    if not source_columns or not required.issubset(source_columns):
        plan["raw_captures"]["skipped_reason"] = (
            "source has no usable 'raw_captures' table (missing table or "
            "required columns); nothing to carry as provenance"
        )
        return

    available_columns = [c for c in RAW_CAPTURE_COLUMNS if c in source_columns]
    select_sql = (
        f"SELECT {', '.join(available_columns)} FROM raw_captures "
        "ORDER BY received_at ASC, id ASC"
    )
    scanned = 0
    for row in source.execute(select_sql):
        scanned += 1
        if scanned % _PROGRESS_INTERVAL == 0:
            _progress(f"raw_captures: {scanned} scanned")
        record = dict(zip(available_columns, row))
        capture_id = record.get("id")
        content = record.get("content")
        received_at = record.get("received_at")
        if not capture_id or not content or received_at is None:
            continue
        if capture_id in existing_raw_capture_ids:
            plan["raw_captures"]["already_present"] += 1
            continue

        memory_id = record.get("memory_id")
        if memory_id is not None and memory_id not in existing_memory_ids:
            # Points at a memory that was filtered out or never existed in
            # the target; keep the capture as orphaned provenance rather
            # than dropping it or forcing a bad foreign key.
            memory_id = None

        updated_at = record.get("updated_at")
        if updated_at is None:
            updated_at = received_at

        plan["raw_captures"]["would_import"] += 1
        existing_raw_capture_ids.add(capture_id)

        if target is not None:
            # Same reasoning as _insert_memory: omit legacy NULLs instead of
            # inserting them explicitly, since several columns here
            # (status, source, project, agent, error, ...) are
            # NOT NULL DEFAULT <x> in the current schema.
            to_insert = {
                c: record[c] for c in available_columns if record.get(c) is not None
            }
            to_insert["memory_id"] = memory_id
            to_insert["updated_at"] = updated_at
            insert_columns = list(to_insert.keys())
            placeholders = ", ".join("?" for _ in insert_columns)
            target.execute(
                f"INSERT INTO raw_captures ({', '.join(insert_columns)}) "
                f"VALUES ({placeholders})",
                [to_insert[c] for c in insert_columns],
            )
