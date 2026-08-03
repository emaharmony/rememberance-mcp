# Context-pack debugging

Use the local, workerless admin commands first:

```text
recall-admin context inspect <context-pack-id>
recall-admin context explain <context-pack-id>
```

`inspect` prints identity, scope IDs, policy versions, source fingerprint,
token totals, expiry, and disposition counts without captured content.
`explain` adds inclusion, omission, scope-filter, and allocation reasons. A
missing pack returns structured `not_found` and a nonzero exit.

Readiness exposes `context_service` with service, migration, feedback-linkage,
policy, and estimator status. `recall-admin doctor` exposes the same fields
without starting the outbox worker. `/stats` contains content-free counters.

Prometheus output includes `recall_context_packs_built_total`,
`recall_context_pack_failures_total`,
`recall_context_pack_build_latency_seconds`,
`recall_context_pack_tokens_estimated_total`,
`recall_context_pack_budget_exceeded_total`,
`recall_context_pack_references_total`,
`recall_context_pack_validation_requests_total`,
`recall_context_pack_scope_rejections_total`, and
`recall_context_pack_feedback_pending_total`. They have no user, repository,
task, path, or captured-text labels.

Common warnings are `mandatory_budget_exceeded`, `continuity_summarized`,
`invalid_version`, `version_too_old`, and `unsupported_capability`. Invalid or
old checkpoint versions return the latest safe checkpoint and an explicit
warning; they never pretend the client is current.

Reference expansion and feedback require the same scope as the pack. A 404 is
intentional for mismatched scope. Pack persistence failure rolls back the pack,
items, references, and usage signals without changing memory content, task
state, or session history.
