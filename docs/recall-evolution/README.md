# Recall Evolution Reference

This directory is the architectural and product guardrail for Recall.

It exists to keep implementation aligned with the product goal and to prevent Recall from drifting into a collection of loosely connected AI infrastructure experiments.

## North Star

> **Recall should become the shared, evidence-backed context layer that lets Claude Code, Codex, Prism, OpenClaw, and other agents continue work without requiring the user to repeatedly rebuild context.**

## Core product promise

A task should be able to move between agents while preserving:

- User intent
- Active task state
- Approved decisions
- Repository evidence
- Constraints
- Progress
- Failed approaches
- Open questions
- Relevant Recall Skills
- Provenance and trust

The user should not need to explain the same project repeatedly.

## Reading order

1. [`01_NORTH_STAR.md`](01_NORTH_STAR.md)
2. [`02_ARCHITECTURE.md`](02_ARCHITECTURE.md)
3. [`03_MVP_AND_ROADMAP.md`](03_MVP_AND_ROADMAP.md)
4. [`04_RISKS_AND_GUARDRAILS.md`](04_RISKS_AND_GUARDRAILS.md)
5. [`05_DECISIONS.md`](05_DECISIONS.md)
6. [`06_SUCCESS_METRICS.md`](06_SUCCESS_METRICS.md)
7. [`07_HANDOFF_SKILLS.md`](07_HANDOFF_SKILLS.md)
8. [`08_ANTI_DRIFT_CHECKLIST.md`](08_ANTI_DRIFT_CHECKLIST.md)

Implementation records:

- [`implementation-notes/phase-0-formal-migrations.md`](implementation-notes/phase-0-formal-migrations.md)
- [`implementation-notes/phase-0-transactional-outbox.md`](implementation-notes/phase-0-transactional-outbox.md)

## Current progress

Phase 1 shared task and session continuity is complete on top of the verified
transactional-outbox checkpoint. Phase 2 retrieval feedback and utility is the
active implementation phase. Deferred Phase 0 refinements remain documented;
they are not silently marked complete.

## One-line architecture

```text
Raw memory
→ utility scoring
→ RAG retrieval
→ stable knowledge
→ versioned Recall Skill
→ cached context
→ cross-agent handoff
```

## Five rules protecting the project

1. **Reliable retrieval comes before sophisticated automation.**
2. **A skill is a compiled view over evidence, not the evidence itself.**
3. **The user approves important truth; agents propose it.**
4. **Scope filtering happens before ranking.**
5. **Do not repeatedly send knowledge that can be referenced by skill ID and version.**

## First undeniable demonstration

```text
Claude Code begins a task
→ Recall records the task and session
→ Recall compiles a handoff skill
→ Codex continues the task
→ Codex reports a structured delta
→ Claude Code resumes without the user restating context
```

If this workflow is reliable and measurably saves context, the product thesis is validated.
