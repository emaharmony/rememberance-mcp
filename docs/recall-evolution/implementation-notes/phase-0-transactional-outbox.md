# Phase 0 Implementation Note: Transactional Outbox

## Current phase

Phase 0 - Reliability foundation (`In progress`).

## Chosen slice

Persist every raw capture and one `capture.process` outbox job in the same
SQLite transaction, then process that job with leased, retryable,
at-least-once delivery.

## Why this slice is next

Recall currently persists raw input before submitting work to an in-memory
executor. A process exit after the raw commit but before or during executor
work can leave a pending capture with no durable recovery path. The formal
migration runner completed in the previous slice is the prerequisite for a
versioned outbox schema.

## Schema and state model

- Migration 5 adds persisted capture overrides and gate results to
  `raw_captures`.
- `outbox_jobs` owns one unique `capture.process` job per raw capture.
- Job states are `pending`, `processing`, `retry`, `complete`, and `dead`.
- Claims increment attempts and receive a lease. Expired leases are
  reclaimable after a process exit.
- A nullable unique `facts.derivation_key` makes capture-derived facts
  idempotent across retries.
- Existing pending raw captures are backfilled into the outbox.

## API and compatibility

- `MemoryPipeline.capture()` keeps its existing arguments and response shape.
- It waits up to `RECALL_CAPTURE_PROCESSING_TIMEOUT`, then returns either the
  completed result or a durable `pending` result.
- REST continues to return HTTP 201; MCP and Prism adapters keep their current
  wire contracts.
- NATS may acknowledge after the raw capture and outbox row commit because the
  remaining work is durable and retryable.

## Processing and failure rules

- Remote/model work never runs inside the enqueue transaction.
- The first gate result is persisted and reused on retry.
- Memory creation, graph wiring, fact extraction, and embeddings are made
  idempotent for at-least-once execution.
- Retryable failures use bounded exponential backoff. Exhausted work becomes
  `dead`, and the raw capture plus provisional memory become `failed`.
- Failure text is sanitized before persistence or exposure. Outbox, raw,
  memory, waiter, health, and log paths retain the exception class and a
  generic message without the raw exception or captured content.
- Job completion and raw-capture completion commit together.
- Queue saturation leaves durable work pending instead of rejecting it.

## Operations

- Health and stats expose stable five-state counts, due age, active leases,
  live worker state, and the sanitized last dispatcher error. Prometheus uses
  a boolean dispatcher-error gauge rather than error text.
- Doctor remains workerless: it reports queue state and explicitly marks live
  worker liveness/error as not observed.
- `recall-admin outbox status` reports backlog without captured content.
- `recall-admin outbox retry <job-id>` requeues one dead job.
- Rollback restores the pre-migration backup and previous application
  artifact; there is no down migration.

## Tests required

- Migration, legacy backfill, and enqueue atomicity
- Concurrent claim exclusivity and expired-lease recovery
- Restart recovery and retry/dead behavior
- Idempotency after partial memory, graph, fact, and embedding work
- Synchronous and timeout/pending capture compatibility
- NATS post-commit acknowledgment behavior
- Worker health, metrics, admin status/retry, and graceful shutdown
- Sanitized failures and retries after each memory, graph, fact, and embedding
  write boundary
- Full test, lint, format, type, compile, build, and clean-wheel checks

## Explicit non-goals

- General client-supplied capture idempotency keys
- Shared application error taxonomy
- Context-use telemetry
- Phase 1 identity, task, or session models
- Redis, PostgreSQL, distributed locks, or multiple Recall writers
- Exactly-once delivery

## Completed checkpoint (2026-08-01)

The transactional-outbox slice is complete on
`feature/recall-phase-0-transactional-outbox`. Phase 0 remains in progress;
no Phase 1 identity or task model was introduced.

Validation evidence:

- Full branch-coverage suite: 220 passed, 2 skipped, 2 expected deprecation
  warnings.
- Branch coverage: 65% repository-wide; the new outbox module is 84% and the
  revised pipeline is 86%.
- Transactional-outbox focus: 16 passed, including rollback, competing claims,
  lease recovery, persisted gate reuse, capped backoff, retry/dead-letter,
  restart, sanitized errors, clean shutdown, and idempotency after every
  derived-write boundary.
- Ruff format/check: clean across 62 files; mypy: no issues in 41 source
  files; `compileall`: clean.
- Source distribution and wheel build succeeded. A clean, no-dependency wheel
  install imported `recall_mcp.outbox` from site-packages, migrated a fresh
  database from schema 0 to 5, reran 5 to 5 with no changes, and executed
  `recall-admin outbox status` successfully.

Known boundary: leases are intentionally process-local with no heartbeat and
assume one Recall writer; the 300-second default exceeds current processing
timeouts. Distributed coordination remains outside Phase 0.

Recommended next slice: general capture idempotency keys at API boundaries,
planned separately before implementation.
