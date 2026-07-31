# Phase 0 Implementation Note: Formal SQLite Migrations

## Current phase

Phase 0 — Reliability foundation (`In progress`).

## Chosen slice

Replace constructor-owned, exception-driven SQLite schema evolution with one
ordered, transactional migration runner and an auditable `schema_migrations`
ledger.

## Why this slice is next

The current schema is assembled by `MemoryStore`, `MemoryStoreV2`,
`EntityStore`, and `FactStore` through independent `CREATE TABLE` and
`ALTER TABLE` calls. `recall-admin migrate` instantiates the complete
application instead of reporting schema work. This makes migration order,
partial failures, concurrency, and legacy upgrades difficult to observe and
test. A formal migration foundation is required before adding the
transactional outbox.

## Files likely affected

- `src/recall_mcp/store/migrations.py`
- `src/recall_mcp/store/{store,memory,edges,facts}.py`
- `src/recall_mcp/admin.py`
- Migration-focused tests and Recall evolution documentation

## Schema changes

- Add `schema_migrations(version, name, applied_at)`.
- Define four ordered migrations:
  1. `core_memory`
  2. `memory_v2`
  3. `graph_and_facts`
  4. `production_reliability`
- Apply each migration and its ledger record in the same
  `BEGIN IMMEDIATE` transaction.
- Keep FTS5 as derived, rebuildable state outside the canonical migration
  ledger.

## API changes

- Keep the `recall-admin migrate` command name.
- Return structured JSON containing the database path, starting version,
  resulting version, and migrations applied.
- Add schema version/freshness fields to integrity and doctor output.
- Do not change REST or MCP contracts.

## Migration risks

- Unversioned databases may contain any subset of the historical schema.
- Concurrent startup must not duplicate or interleave migrations.
- A failed migration must not leave its DDL or ledger entry committed.
- Databases with unknown future or non-contiguous versions must fail closed.
- Existing rows and compatibility-layer behavior must remain unchanged.

## Compatibility requirements

- Fresh databases and existing unversioned databases follow the same runner.
- Schema changes remain additive; no table, column, or memory-domain field is
  renamed.
- Startup continues to initialize compatible databases automatically.
- Rollback remains restoration of the matching pre-migration backup and prior
  application artifact.
- Python 3.10 and the existing dependency set remain supported.

## Tests required

- Fresh database migration
- Legacy and partially upgraded database migration with data preservation
- Idempotent rerun
- Concurrent migration attempts
- Transaction rollback on failure
- Unknown and non-contiguous version rejection
- Admin CLI JSON and integrity reporting
- Full existing test, lint, format, type, build, and clean-wheel checks

## Explicit non-goals

- Transactional outbox
- General capture idempotency keys
- Phase 1 identity, task, or session models
- Down migrations or automatic rollback
- PostgreSQL, Redis, or Alembic
- FTS ranking or rebuild redesign
- REST or MCP contract redesign

## Completed checkpoint

- The four canonical migrations, ledger, startup integration, admin reporting,
  and integrity reporting are implemented.
- Fresh, legacy-unversioned, partially migrated, concurrent, failed, and
  incompatible-history paths are covered by 10 focused migration tests.
- The complete suite passes: 197 passed, 2 skipped, with the 2 expected
  compatibility warnings.
- Branch-aware coverage remains 62%; the new migration module is 88% covered.
- Ruff lint and formatting, mypy across 42 source files, and bytecode
  compilation pass.
- The source distribution and wheel build successfully. A clean virtual
  environment installed the wheel and verified both the `0 -> 4` migration
  and the idempotent `4 -> 4` rerun.
- Existing legacy tables are upgraded additively. Tables created before
  foreign-key constraints existed are not rebuilt; cleanup triggers preserve
  deletion behavior for those installations.
- The transactional outbox remains deliberately deferred and is the
  recommended next Phase 0 slice.
