# Schema migrations

The current schema is version 12. Migration 11 added disposable CAG cache
metadata and immutable delivery telemetry. Migration 12 adds `memory_chunks`
for per-chunk embeddings, used by chunk-on-write and chunk-level vector search
(both gated behind `RECALL_CHUNKING_ENABLED`, default off).

Supported checks are clean `0 -> 12`, incremental `11 -> 12`, and idempotent
`12 -> 12`. Existing memories, sessions, retrieval/utility telemetry, Context
Pack V2 rows, approved skills, handoffs, and CAG cache rows are not rewritten.
Rollback remains restore from a verified backup and run the prior application
artifact; down migrations are intentionally unsupported.

Recall uses ordered, forward-only SQLite migrations for canonical schema
changes.

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
| 7 | `retrieval_utility` | Retrieval telemetry, context usage, task outcomes, explainable utility history, and reversible lifecycle state |
| 8 | `context_pack_v2` | Reproducible V2 snapshots, scope and policy metadata, dispositions, references, fingerprints, and token accounting |
| 9 | `versioned_skills` | Scope-aware skills, immutable versions and sources, exact-version review audit, usage feedback, and Context Pack linkage |
| 10 | `agent_handoffs` | Immutable target-agent handoffs, lifecycle audit, structured completion, references, and session updates |
| 11 | `cag_context_cache` | Disposable scoped cache metadata and dependencies, immutable CAG deliveries and feedback, and invalidation audit |
| 12 | `memory_chunks` | Per-chunk embeddings for long-memory vector search: chunk content, embedding, model, and dimensionality, cascade-deleted with their parent memory |

Migration 11 cache rows are rebuildable optimization metadata. Delivery records
remain an audit of what was sent, but neither cache table becomes canonical
task, memory, session, skill, or handoff truth.

Migration 12 `memory_chunks` rows are also rebuildable derived state (from
`memories.content`, via the dream `chunk_backfill` phase or chunk-on-write) —
not independent canonical truth, and they carry no scope columns of their own;
they inherit scope from their parent `memories` row through `memory_id`.

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
