# Utility Scoring

Admission and long-term utility are separate. The ingestion gate accepts a
durable candidate as `ephemeral`, `session`, or `active`; it does not declare
that candidate important forever. Retention may later assign `pinned`,
`stable`, `active`, `session`, `ephemeral`, `cold`, or `archived`.

## Policy `utility-v1`

The deterministic score is clamped to `[0, 1]`:

```text
initial importance
+ successful reuse + cross-session reuse + cross-agent reuse
+ task success + trust + freshness + pinned
- staleness - duplicate - ignored - correction - contradiction
```

Every persisted change appends the previous score, new score, reason,
component values, and policy version to `utility_history`. Identical database
state, policy, and calculation time produce an identical score. No LLM is
required to calculate utility, and model confidence is not verification.

Weights are overridden with `RECALL_UTILITY_WEIGHTS`, a JSON object whose keys
must name existing components. `RECALL_UTILITY_POLICY_VERSION` identifies the
policy used for current and historical explanations.

## Retention and lifecycle

`last_retrieved_at`, `last_selected_at`, `last_injected_at`,
`last_expanded_at`, `last_used_at`, and `last_successful_use_at` preserve the
different evidence. Retention extensions default to 1, 3, 14, 30, and 90 days
for selection, injection, expansion, use, and successful use. Review defaults
to 30 days.

Pinned memories have no automatic expiry. Expired unpinned memories are
reversibly demoted to `cold`, never permanently deleted. Normal retrieval
excludes cold and archived memories before ranking. Explicit search with
`include_cold=true` can inspect them, and restore returns them to active
eligibility. Pin, unpin, cold, and restore transitions are audited.

Automatic permanent pruning and skill promotion are outside this phase.
