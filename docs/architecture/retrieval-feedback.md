# Retrieval Feedback

Recall records retrieval as an observable lifecycle without treating retrieval
alone as evidence of usefulness:

```text
candidate -> selected -> injected -> expanded -> used -> task outcome
```

Each state is distinct. A candidate updates `last_retrieved_at` but never
extends expiry. Selection and injection have small configurable retention
effects; expansion and use have larger effects; successful task use has the
strongest automatic extension. Ignore, rejection, correction, and rework are
negative evidence.

## Durable records

- `retrieval_runs` records the permitted scope, query mode, limit, agent, and
  latency after scope filtering has been applied.
- `retrieval_results` records rank and only the keyword, vector, graph, and tier
  score components that actually ran. Missing paths remain `NULL`.
- `context_packs` gives selected context a durable identity.
- `context_usage` records returned, selected, injected, expanded, used,
  ignored, corrected, and rejected events with agent attribution.
- `task_outcomes` records success, corrections, rework, and the reporting agent.

Telemetry idempotency keys reject conflicting replay. A context-use event must
refer to a memory in that context pack, and all utility reads require the same
user/project/repository/task scope as the memory. Telemetry failure is reported
through stats, readiness diagnostics, doctor output, logs, and Prometheus, but
does not roll back a canonical memory or change the capture acknowledgement.

## Shared surfaces

REST exposes feedback at `/v2/context/{id}/feedback`,
`/v2/retrieval/{id}/feedback`, and `/v2/tasks/{id}/outcome`. Utility inspection
uses `/v2/memories/{id}/utility` and `/utility/history`. MCP clients use
`recall_context_feedback`, `recall_task_outcome`, and
`recall_memory_utility`; all transports call `RetrievalFeedbackService`.

Known limitation: agent and client adapters must explicitly report expansion,
use, correction, rejection, and task outcomes. Recall does not infer these
signals from model confidence.
