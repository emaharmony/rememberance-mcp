# Recall Decisions

This document records the currently approved product and architectural direction.

## 1. Recall's role

Recall is the central context-management layer.

Recall owns:

- Storage
- Retrieval
- Ranking
- Caching
- Pruning
- Skill compilation
- Session continuity
- Trust and provenance
- Validation requests

Prism remains the workflow orchestrator.

## 2. RAG and CAG

Recall will support both:

- Retrieval-Augmented Generation
- Cache-Augmented Generation

The preferred behavior is hybrid RAG plus CAG:

```text
Load stable cached skills
→ validate them
→ retrieve only changed or missing evidence
```

## 3. Cross-system content

Recall may preserve:

- Architectural decisions
- Active task state
- Failed approaches
- User preferences
- Repository summaries
- Coding conventions
- Open bugs and risks
- Conversation summaries
- Tool results
- Agent hypotheses

Different categories receive different trust, retention, and validation rules.

## 4. Session continuity

Default:

```text
Compact task and session package
```

On demand:

```text
Deeper evidence retrieval
```

## 5. Scope hierarchy

```text
User
→ Workspace
→ Project
→ Repository
→ Task
→ Session
```

Agents participate in sessions.

## 6. Recall Skills

Recall Skills may render as:

- Markdown
- JSON
- Instruction bundle
- MCP resource
- Expandable reference
- Agent-specific handoff

One canonical internal representation supports all renderers.

## 7. Skill creation

Initial mode:

```text
Automatic suggestion
→ Manual approval
```

Long-term mode:

```text
Automatic publication
when measured trust and usefulness exceed thresholds
```

Automation must not rely only on model confidence.

## 8. Reuse and utility

Use combined signals:

- Retrieval frequency
- Cross-session reuse
- Cross-agent reuse
- Successful-task contribution
- Manual pinning
- Trust
- Freshness
- Duplicate penalties
- Staleness
- Ignored-result history

## 9. Retention lifecycle

```text
Pinned
→ Stable
→ Active
→ Session
→ Ephemeral
→ Cold
→ Archived
→ Purged
```

## 10. Pruning

Prefer suppression, demotion, and archival before permanent deletion.

## 11. Context delivery

Always inject:

- Active task
- Approved decisions
- Critical constraints
- Compact skill summaries
- Current blockers

Reference on demand:

- Long evidence
- Historical detail
- Raw transcripts
- Large repository summaries

## 12. Token efficiency

Support:

- Unchanged-context suppression
- Skill ID and version references
- Client caches
- Deltas
- Hierarchical summaries
- Token-savings telemetry

## 13. Caching

Cache in both Recall and clients.

Initial design:

```text
SQLite durable metadata
+ in-process LRU cache
+ client skill-version cache
+ NATS invalidation
```

Redis is optional later.

## 14. Expiration

Extend expiry when content is:

- Updated
- Selected
- Confirmed as used
- Associated with a successful task

Do not extend expiry merely because it appeared as a retrieval candidate.

## 15. Contradictions

When claims conflict:

1. Preserve both
2. Mark the contradiction
3. Gather evidence
4. Generate pros and cons
5. Request user validation
6. Save the decision and reason
7. Update canonical fact state
8. Refresh affected skills

## 16. Write permissions

Write authority is configurable by agent.

A dedicated Recall Curator Agent evaluates memory writes and maintenance.

## 17. Authority and evidence

### Decision authority

```text
User-approved decision
> explicit policy
> approved plan
> delegated decision
> unapproved recommendation
```

### Factual evidence

```text
Repository and tests
> successful tool output
> versioned documentation
> user factual statement
> trusted agent conclusion
> unverified agent statement
> external content
```

## 18. Transports

Support:

- REST
- MCP
- NATS
- Local library access when useful

All transports share one application layer.

## 19. Initial clients

1. Prism
2. Claude Code
3. Codex
4. OpenClaw
5. Other agents later

## 20. Repository access

Recall may receive read-only access to explicitly registered repositories.

It may incrementally and event-driven scan repositories.

It should not autonomously modify repositories.

## 21. Initial scale

```text
One user
Approximately five systems
Multiple agents
Fewer than roughly 100,000 memories
```

## 22. Success priorities

1. Cross-agent continuity
2. Fewer repeated explanations
3. Retrieval accuracy
4. Token reduction
5. Task completion
6. Factual consistency

Scope leakage is a zero-tolerance guardrail.

## 23. First milestone

Build a thin end-to-end version of:

- Shared sessions
- Context Pack V2
- Approved skills
- Handoff skills
- Client version cache
- Retrieval feedback
- Cold demotion
- Contradiction validation

## 24. Formal SQLite migrations

Recall Local uses an in-process, dependency-free SQLite migration runner with
an auditable `schema_migrations` ledger.

Approved behavior:

- Migrations are ordered, additive, and forward-only.
- One migration and its ledger record form one transaction.
- Startup and `recall-admin migrate` use the same runner.
- Unversioned legacy databases are inspected and upgraded without replacing
  canonical rows.
- Unknown future, renamed, or non-contiguous histories fail closed.
- FTS5 remains derived state outside the canonical ledger.
- Rollback uses backup restoration rather than down migrations.

Alembic and other migration dependencies are not justified for the current
single-user SQLite target.
