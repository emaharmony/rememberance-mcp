# Recall Evolution Task Context

## Task

Advance the earliest incomplete phase in the Recall evolution roadmap by one coherent, production-quality slice, with tests, validation, and progress documentation, then stop.

## Binding gates

- Read the authoritative documents under `docs/recall-evolution/` before editing.
- Verify Recall is canonical: package `recall_mcp`, CLI `recall`, environment prefix `RECALL_`, and home `~/.recall`.
- If the rename is incomplete or the reference documents are missing, stop without roadmap implementation.
- Preserve unrelated worktree changes.
- Do not push, merge, publish, release, or implement more than one coherent slice.

## Required preflight

- Repository root, branch, status, version.
- Baseline tests, lint, formatting, type checks, and applicable build checks.
- Inspect schemas, migrations, stores, pipeline/application services, transports, retrieval/context logic, tests, and docs relevant to the selected phase.

## Architecture constraints

- Canonical writes must be durable before acknowledgment.
- Scope filtering precedes ranking.
- Canonical and derived data remain separated; derived work is rebuildable and retryable.
- REST, MCP, NATS, and library transports share application/domain rules.
- SQLite plus in-process/client caches and a transactional outbox are the MVP baseline.
- No Redis, PostgreSQL, broad refactors, or unrelated roadmap work without measured current-phase need.

## Expected workflow

1. Find the earliest incomplete roadmap phase from repository evidence.
2. Select the smallest coherent slice that advances its exit gate.
3. Record an implementation note covering phase, slice, affected files, schema/API/migration/compatibility concerns, tests, and non-goals.
4. Implement incrementally, add behavior-focused tests, update authoritative docs.
5. Run all applicable validation and report exact results, limitations, roadmap progress, next slice, and anti-drift answers.

## Context-scout status

The required local Ollama (`qwen3.5:9b`) scout was attempted against the attached brief but timed out before returning a usable summary. Per repository instructions, direct context gathering continues with minimal targeted reads.
