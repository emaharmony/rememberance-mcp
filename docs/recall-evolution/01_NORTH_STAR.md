# Recall North Star

## Product identity

Recall is not merely a vector database, transcript archive, or generic knowledge graph.

Recall is:

> **Persistent semantic context infrastructure for AI agents.**

Its purpose is to let multiple AI systems share durable knowledge, current task state, approved decisions, and reusable context without repeatedly rediscovering the same information.

## End goal

Build a reliable RAG and CAG system that can:

- Retrieve relevant evidence for new or unfamiliar work
- Cache frequently reused context
- Avoid repeatedly transmitting unchanged information
- Prune, demote, or suppress irrelevant context
- Preserve approved decisions and historical evidence
- Carry active task state between systems
- Produce reusable, versioned Recall Skills
- Support parent-agent to subagent handoff
- Track provenance, trust, and freshness
- Ask the user to resolve important contradictions

## Desired user experience

The user should be able to:

1. Begin work in Claude Code.
2. Delegate implementation to Codex.
3. Let Prism coordinate the workflow.
4. Consult OpenClaw or another agent.
5. Resume in the original system.

The user should not need to repeat:

- Project architecture
- Current objective
- Decisions
- Constraints
- Work already completed
- Known failures
- Important files
- Open questions

## RAG

Use retrieval when the task needs fresh or unfamiliar evidence.

```text
Question
→ scope resolution
→ keyword, vector, and graph retrieval
→ evidence-backed context
```

## CAG

Use cached, compiled knowledge when information is stable and frequently reused.

```text
Repeated project need
→ stable Recall Skill
→ cached skill ID and version
→ compact delivery
```

## Hybrid RAG and CAG

The preferred long-term path is:

```text
Load stable cached skills
→ validate freshness and source hashes
→ retrieve only missing or changed evidence
→ deliver a context delta
```

## Product boundary

Recall owns:

- Durable memory
- Retrieval
- Context assembly
- Context caching
- Recall Skills
- Session continuity
- Handoff packages
- Trust and provenance
- Utility scoring
- Retention and pruning
- Validation requests
- Read-only repository context

Prism remains the workflow orchestrator.

Recall should not become the universal executor for every workflow.

## Primary proof of value

The first major proof is not a large benchmark dashboard or a sophisticated graph.

It is:

```text
Claude Code starts a task
→ Recall creates shared task/session state
→ Recall builds a handoff skill
→ Codex continues without a repeated explanation
→ Codex returns a structured delta
→ Claude Code resumes correctly
```

## Product moat

The likely moat is not:

- Embeddings
- Vector storage
- A knowledge graph
- A generic cache

The moat is:

> **Reliable, evidence-backed, versioned continuity between heterogeneous AI agents.**

That includes:

- Task-aware context
- Handoff packaging
- Skill versioning
- Delta delivery
- Trust and provenance
- Feedback-driven retention
- Cross-client interoperability
