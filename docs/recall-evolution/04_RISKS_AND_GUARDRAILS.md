# Recall Risks and Guardrails

## Honest assessment

The product idea is strong and technically feasible.

The greatest risk is not that the idea lacks value.

The greatest risk is that Recall attempts to become too many products before its core continuity workflow is reliable.

## Risk 1 — Platform sprawl

Recall could become all of these at once:

- Memory database
- RAG engine
- CAG cache
- Session coordinator
- Skill compiler
- Handoff protocol
- Knowledge graph
- Repository indexer
- Contradiction detector
- Approval system
- Curator agent
- Retention engine
- Evaluation platform
- Transport layer

### Guardrail

Do not add a subsystem unless it measurably improves at least one of:

- Cross-agent continuity
- Repeated-explanation reduction
- Retrieval quality
- Token usage
- Task completion
- Factual consistency

## Risk 2 — Skills becoming stale summaries

A compiled skill can save tokens while becoming confidently wrong.

### Required protection

Every skill needs:

- Evidence references
- Source hashes
- Repository commit references
- Immutable versions
- Freshness state
- Invalidation rules
- Trust state
- Expansion back to evidence

### Rule

> A skill is a compiled view over evidence, not the evidence itself.

## Risk 3 — Cache invalidation complexity

Context may depend on:

- User
- Workspace
- Project
- Repository
- Branch
- Commit
- Task
- Session
- Agent role
- Skill versions
- Policy version

### Guardrail

Do not treat fast storage as the primary caching problem.

The primary problem is determining whether cached context is still valid.

Redis should not be introduced before the validity model is proven.

## Risk 4 — Automatic pruning destroying useful history

Rarely used memories can remain important:

- Security decisions
- Failed migrations
- Architectural constraints
- Rare user preferences
- Rejected approaches
- Bugs that later return

### Guardrail

Use:

```text
Normal retrieval
→ Cold
→ Archived
→ Review
→ Purged
```

Do not use:

```text
Not recently retrieved
→ Immediate permanent deletion
```

Recall should become good at suppression before aggressive deletion.

## Risk 5 — Session bleed becoming scope leakage

Semantic relevance does not grant access.

### Guardrail

Scope filtering happens before ranking.

Cross-project and cross-user leakage is a security failure, not merely a quality issue.

## Risk 6 — Agent suggestions becoming accepted truth

An agent suggestion can later be presented as fact if categories are not preserved.

### Guardrail

Keep separate:

```text
Fact
Decision
Recommendation
Hypothesis
External claim
Tool result
```

Important truth requires:

- User approval
- Repository evidence
- Successful tool output
- Explicit project policy

## Risk 7 — Advanced features hiding weak retrieval

The project could gain many subsystems while retrieval still returns noisy context.

### Guardrail

Before sophisticated automation, Recall must be excellent at:

1. Capturing useful information
2. Preserving durable writes
3. Enforcing scope
4. Retrieving correct evidence
5. Building useful context packs
6. Measuring whether the context helped
7. Passing context between agents

## Risk 8 — Curator Agent overreach

The Curator is appropriate for:

- Classification
- Duplicate suggestions
- Candidate skill generation
- Retention recommendations
- Contradiction detection
- Validation requests
- Handoff packaging

It should not independently:

- Override the user
- Approve major contradictions
- Promote external claims to canonical truth
- Purge pinned memories
- Rewrite repositories
- Treat its own confidence as evidence

### Rule

```text
Deterministic rules define authority.
The Curator analyzes and recommends.
The user validates important uncertainty.
```

## Risk 9 — Measuring rankings instead of outcomes

A high-ranked memory may still be useless.

Track:

```text
Retrieved
Selected
Injected
Used
Helped task
Ignored
Corrected
Caused regression
```

## Risk 10 — Endless abstraction

Avoid designing generalized infrastructure for hypothetical clients before three clients work well.

Priority order:

1. Prism
2. Claude Code
3. Codex
4. OpenClaw
5. Additional clients later

## Risk 11 — Overcomplicated trust scoring

A single numeric trust score can hide important distinctions.

### Guardrail

Preserve:

- Source type
- Verification method
- Approval state
- Evidence references
- Contradiction state
- Decision authority

Do not let one score replace provenance.

## Risk 12 — Recall replacing Prism

Recall may begin coordinating too much workflow behavior.

### Guardrail

Recall manages context and memory.

Prism manages execution, orchestration, gates, and workflow sequencing.
