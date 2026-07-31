# Recall Target Architecture

## 1. Ownership and scope hierarchy

Use:

```text
User
└── Workspace
    └── Project
        └── Repository
            └── Task
                └── Session
```

Agents are participants in sessions rather than owners of isolated task copies:

```text
Session
├── Claude Code
├── Codex
├── Prism
└── OpenClaw
```

This permits work to move between systems while preserving one task history.

## 2. Scope metadata

Records may carry:

```text
user_id
workspace_id
project_id
repository_id
task_id
session_id
agent_id
branch
commit_sha
scope
```

Not every record requires every field, but the model must support them.

Scope must be resolved before retrieval.

Correct:

```text
Resolve allowed scope
→ retrieve permitted data
→ rank results
```

Incorrect:

```text
Search all data
→ rank results
→ remove unauthorized results afterward
```

## 3. Authority and evidence

Do not collapse authority and evidence into one trust number.

### Decision authority

```text
User-approved decision
> explicit project policy
> approved technical plan
> delegated agent decision
> unapproved agent recommendation
```

### Factual evidence

```text
Current repository and test results
> successful tool output
> versioned project documentation
> user-provided factual statement
> trusted agent conclusion
> unverified agent statement
> external retrieved content
```

This allows Recall to preserve both:

```text
Current fact:
SQLite is currently used.

Approved direction:
PostgreSQL will be added for Recall Server.
```

## 4. Canonical versus derived data

### Authoritative

```text
Canonical memories
Facts and temporal fact history
Approvals
Ownership and scope
Provenance
Tasks
Sessions
Session events
```

### Derived

```text
FTS index
Embeddings
Chunks
Automatically generated graph links
Compiled truth
Recall Skills
Context caches
Repository summaries
```

Derived data must be:

- Rebuildable
- Observable
- Retryable
- Invalidatable
- Connected to source evidence

## 5. Capture and enrichment flow

```text
Capture candidate
    ↓
Resolve identity and scope
    ↓
Ingestion gate
    ↓
Canonical memory transaction
    ↓
Transactional outbox
    ↓
Extraction, embedding, chunking, graph, facts
    ↓
Utility scoring
    ↓
Retrieval feedback
    ↓
Skill compilation
```

Remote model calls should not occur inside long database transactions.

## 6. Shared application layer

```text
REST ─┐
MCP  ─┼→ Recall application services → repositories
NATS ─┘
```

Transports must not implement separate:

- Capture policies
- Ranking rules
- Trust policies
- Scope rules
- Skill logic
- Retention logic

## 7. Recall Skills

A Recall Skill is a reusable, versioned context package backed by evidence.

```yaml
id: skill:prism:architecture
version: 7
title: Prism Architecture
purpose: Stable architecture guidance for coding agents

scope:
  user_id: ema
  project_id: prism
  repository_id: emaharmony/prism

summary: >
  Prism is a workflow-first, event-driven AI orchestration framework.

instructions:
  - Preserve workflow-first design
  - Keep persistent memory in Recall
  - Use NATS for event transport
  - Do not bypass approval gates

facts:
  - statement: Prism's core runtime is written in Go
    trust: verified

open_questions: []

evidence:
  - type: repository
    ref: commit:abc123
  - type: memory
    ref: memory:01J...

token_estimate: 680
content_hash: sha256:...
source_hash: sha256:...
created_at: ...
updated_at: ...
expires_at: ...
```

A skill must support:

- Immutable versions
- Source references
- Source hashes
- Freshness state
- Invalidation
- Expansion to evidence
- Usage tracking
- Token estimates
- Scope enforcement

## 8. Context Pack V2

A Context Pack should include:

```text
Scope
Active objective
Task state
Session delta
Inline context
Skill references
Retrieved evidence
Open questions
Validation requests
Token budget
Tokens saved
Cache status
Provenance
Trust labels
```

Always inject:

- Active objective
- User-approved decisions
- Critical constraints
- Current blockers
- Compact skill summaries
- Relevant validation requests

Reference rather than fully inject:

- Long documents
- Raw transcripts
- Historical tool output
- Detailed supporting evidence
- Large repository summaries

## 9. Client version negotiation

Clients report known versions:

```json
{
  "known_skills": {
    "skill:prism:architecture": 6
  }
}
```

Recall can return:

```json
{
  "skill": "skill:prism:architecture",
  "from_version": 6,
  "to_version": 7,
  "delta": {
    "added": ["Recall handoff skills are now versioned"],
    "removed": [],
    "changed": []
  }
}
```

## 10. Utility scoring

The ingestion gate asks:

> Should this candidate enter Recall?

The utility scorer asks:

> How useful is this memory now?

Utility may consider:

```text
Initial importance
Successful reuse
Cross-session reuse
Cross-agent reuse
Successful task contribution
Trust
Freshness
User pinning
Novelty
Duplication
Contradictions
Ignored-result history
Age
Completed-task decay
```

## 11. Retention lifecycle

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

The initial gate should not permanently decide retention.

## 12. Expiration behavior

Track separately:

```text
retrieved_at
selected_at
used_at
successful_use_at
updated_at
```

Extend expiry when information is:

- Updated
- Selected into a context pack
- Confirmed as used
- Associated with successful task completion

Do not extend expiry merely because a poor retriever returned it as a candidate.

## 13. Repository awareness

Recall may support explicitly registered, read-only repositories.

Autonomy levels:

```text
0 — Disabled
1 — On demand
2 — Incremental
3 — Event driven
4 — Curated autonomy
```

The near-term goal is Level 1, then Level 3.

Recall may:

- Read registered files
- Detect changed commits
- Index important documentation and symbols
- Update repository summaries
- Invalidate affected skills
- Suggest new skills

Recall should not autonomously edit repositories.

## 14. Formal schema migration boundary

Canonical SQLite schema changes use an ordered migration ledger. Each migration
and its ledger record commit in the same `BEGIN IMMEDIATE` transaction.
Unknown future or non-contiguous histories fail closed.

Store constructors and `recall-admin migrate` share this runner. Optional FTS5
indexes remain outside the canonical ledger because they are derived and
rebuildable. Rollback restores the matching pre-migration database backup and
prior application artifact; Recall does not run down migrations.
