# Recall MVP and Roadmap

## Guiding principle

Prove cross-agent continuity before building the entire context platform.

Work on the earliest incomplete phase. Do not begin later phases simply because they are more interesting.

## Thin end-to-end MVP

The MVP includes:

1. Shared task and session IDs
2. Claude Code, Codex, and Prism as the first clients
3. Context Pack V2
4. Manually approved Recall Skills
5. One parent-to-subagent handoff workflow
6. Client caching of skill IDs and versions
7. Retrieval feedback
8. Dynamic utility scoring
9. Cold demotion without automatic deletion
10. User validation for contradictions
11. Curator Agent in recommendation-only mode
12. Read-only repository scan on demand

## Excluded from MVP

Do not require:

- Redis
- PostgreSQL
- Multi-user tenancy
- Permanent automatic pruning
- Fully autonomous skill publication
- Autonomous repository writes
- Multi-instance Recall
- Distributed locking
- Sophisticated graph reasoning
- A generalized plugin marketplace

---

## Phase 0 — Reliability foundation

Status: **In progress**

Progress:

- [x] Formal schema migrations
- [x] Transactional outbox
- [x] Foreign-key enforcement
- [ ] Idempotent capture — database-backed NATS events exist; general capture keys remain
- [ ] Structured errors — REST errors exist; shared application errors remain
- [x] Backup and restore
- [ ] Narrow exception handling
- [ ] Usage telemetry — service and gate metrics exist; context-use telemetry remains

Completed slice:

- Four ordered, transactional SQLite migrations
- Auditable schema ledger and fail-closed version validation
- Fresh, legacy, retry, rollback, and concurrent migration coverage
- Structured `recall-admin migrate` reporting
- Atomic raw-capture and outbox enqueue
- Leased, retryable, restart-safe capture processing
- Idempotent memory, graph, timeline, fact, and embedding retries
- Outbox health, metrics, doctor, status, and manual retry operations

Deferred:

- General capture idempotency keys and all later Phase 0 work

Next recommended slice:

- General capture idempotency across REST, MCP, NATS, and library callers

Exit gate:

> Recall never acknowledges a canonical memory write that was not durably stored.

---

## Phase 1 — Identity, tasks, and sessions

Status: **Complete**

Build:

- [x] User/workspace/project/repository/task/session model
- [x] Agent registry
- [x] Session participants
- [x] Append-only session events
- [x] Deterministic checkpoints and ordered deltas
- [x] Cross-agent session lookup through REST, MCP, and context building
- [x] Scope-before-ranking enforcement for formal memory scope

Exit gate:

> Claude Code and Codex can read and update the same task session.

---

## Phase 2 — Retrieval feedback and utility scoring

Status: **Complete**

Build:

- [x] Retrieval-run logging
- [x] Candidate versus selected results
- [x] Selected versus used results
- [x] Task-outcome feedback
- [x] Explainable, versioned utility scoring
- [x] Shadow priority recalibration with zero default influence
- [x] Reversible cold demotion and audited restoration

Exit gate:

> Recall can explain why a memory was promoted, retained, demoted, or suppressed.

---

## Phase 3 — Context Pack V2

Status: **Complete**

Build:

- [x] Scope-aware context assembly
- [x] Token-budget allocation
- [x] Inline-versus-reference decisions
- [x] Active task state
- [x] Session delta
- [x] Future skill-reference extension points
- [x] Provenance
- [x] Trust labels
- [x] Validation notices

Exit gate:

> Prism, Claude Code, and Codex receive equivalent context through their preferred transports.

---

## Phase 4 — Manually approved Recall Skills

Build:

- Canonical skill schema
- Skill compiler
- Skill source tracking
- Immutable versions
- Markdown and JSON renderers
- Manual approval
- Source-hash invalidation
- Skill usage telemetry

Exit gate:

> Stable knowledge can be referenced by skill ID and version instead of being resent in full.

---

## Phase 5 — Handoff Skills

Build:

- Parent-to-subagent handoff compiler
- Target-agent-specific rendering
- Expected-output contracts
- Session checkpoint integration
- Completion deltas
- Skill-version deltas

Exit gate:

> A task moves between Claude Code, Codex, and Prism without full re-explanation.

---

## Phase 6 — CAG caching

Build:

- Compiled context cache
- Client skill-version cache
- Cache dependency tracking
- Sliding expiration after successful use
- Source-hash invalidation
- Context deltas
- Token-savings telemetry

Exit gate:

> Repeated tasks load cached skills and retrieve only changed evidence.

---

## Phase 7 — Curator Agent

Build:

- Write-candidate queue
- Duplicate analysis
- Contradiction detection
- Skill suggestions
- Retention suggestions
- Validation request generation
- User approval workflow
- Controlled autonomous demotion

Exit gate:

> The Curator improves memory quality without silently changing canonical truth.

---

## Phase 8 — Repository indexing

Build:

- Read-only repository registration
- Incremental Git-aware scans
- Symbol-aware chunks
- Commit and branch provenance
- Skill invalidation
- Repository summary skills
- Event-driven updates

Exit gate:

> Recall detects stale skills when repository evidence changes.

---

## Phase 9 — Evaluation and controlled autonomy

Build:

- RAG benchmark
- Handoff benchmark
- Skill usefulness benchmark
- Pruning precision benchmark
- Contradiction benchmark
- Token-savings dashboard
- Curator autonomy thresholds

Exit gate:

> Automation is promoted based on measured quality rather than model confidence.

---

## Infrastructure progression

### Recall Local MVP

```text
SQLite
+ in-process cache
+ client caches
+ transactional outbox
+ NATS invalidation when available
```

### Recall Server

```text
PostgreSQL
+ pgvector
+ optional Redis
```

### Multi-instance Recall

```text
PostgreSQL
+ pgvector
+ Redis strongly recommended
```

## Redis decision

Redis is not required for the initial target:

```text
One user
Approximately five systems
Multiple agents
Fewer than roughly 100,000 memories
```

Add Redis only when there is measured need for:

- Multi-instance cache sharing
- Distributed locking
- Shared TTL state
- Cross-machine cache coordination
- High concurrency
- Divergent local caches

The difficult problem is cache validity, not cache speed.
