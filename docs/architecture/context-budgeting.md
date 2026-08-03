# Context budgeting

Context Pack V2 uses one deterministic provider-neutral estimator and one
allocator. The default estimator, `chars-v1`, estimates one token per four
UTF-8 bytes, rounded up. It is approximate and centralized; no transport
performs its own token approximation.

## Priority

Allocation order is critical warnings, approved decisions, critical
constraints, active task objective and status, blockers, session delta,
completed and remaining work, open questions and failures, scoped evidence,
then supporting references.

Mandatory state is allocated first. Defaults reserve continuity up to 30%,
retrieval up to 45%, references up to 15%, and a 10% response reserve. These
weights are configurable with `RECALL_CONTEXT_BUDGET_WEIGHTS`; mandatory
content is not capped by a percentage.

If mandatory content exceeds the request budget, critical constraints and
approved decisions remain present. Recall deterministically compacts
lower-priority continuity lists, returns `mandatory_budget_exceeded`, and
reports the conflict in `token_usage`. It never silently drops a critical
constraint. The budget measures delivered context content, not JSON envelope
syntax.

## Dispositions

- `inline`: compact mandatory state and essential short evidence.
- `summary`: an existing compact evidence summary when materially smaller.
- `reference`: long evidence deferred behind scoped expansion.
- `omitted`: duplicate or lower-priority evidence that cannot fit; the reason
  and estimated omitted tokens remain visible.

References reserve metadata tokens while reporting the full expansion cost.
The default maximum inline evidence item is 180 estimated tokens. Token totals
are persisted and reproduced in the pack explanation.
