# Schema migrations

Recall uses ordered, forward-only SQLite migrations for canonical schema
changes. The current schema version is `6`.

## Migration ledger

The primary database records successfully applied migrations in:

```sql
schema_migrations(version INTEGER PRIMARY KEY, name TEXT, applied_at REAL)
```

Current migrations are:

| Version | Name | Responsibility |
| --- | --- | --- |
| 1 | `core_memory` | Canonical memory table and base indexes |
| 2 | `memory_v2` | Compiled truth, timeline, dream metadata, and dream log |
| 3 | `graph_and_facts` | Entities, edges, aliases, memory links, and facts |
| 4 | `production_reliability` | Scope, embedding and processing metadata, raw captures, and ingestion events |
| 5 | `transactional_outbox` | Durable capture jobs, leases, retries, persisted gate results, and fact derivation keys |
| 6 | `task_session_continuity` | Formal scope, agents, tasks, sessions, append-only events, and checkpoints |

FTS5 tables and triggers are not canonical migration state. They are derived
indexes that Recall can rebuild from canonical memories.

## Applying migrations

Take and verify a backup before upgrading, then run:

```text
recall-admin migrate
```

The command returns structured JSON:

```json
{
  "database": "C:\\Users\\example\\.recall\\memory.db",
  "from_version": 4,
  "to_version": 5,
  "applied": [
    {
      "version": 5,
      "name": "transactional_outbox"
    }
  ]
}
```

Recall also applies pending migrations during store initialization, preserving
the existing startup behavior. Repeated migration runs are no-ops.

## Safety behavior

- Every migration uses `BEGIN IMMEDIATE`.
- The schema change and its ledger row commit in the same transaction.
- Concurrent migration attempts serialize through SQLite locking.
- Unversioned legacy databases are upgraded through idempotent schema
  inspection without replacing existing rows.
- Unknown newer, renamed, or non-contiguous migration histories fail closed.
- Migration failures stop startup and retain the last successfully committed
  version.
- Foreign keys and the five-second SQLite busy timeout remain enabled.

## Rollback

Recall does not perform down migrations. To roll back:

1. Stop Recall and every writer.
2. Restore the matching pre-migration SQLite backup.
3. Restore the previous application artifact and configuration.
4. Run `recall-admin integrity-check`.
5. Start Recall and verify readiness and a known search.

Never run an older artifact against a database with a newer migration ledger.
