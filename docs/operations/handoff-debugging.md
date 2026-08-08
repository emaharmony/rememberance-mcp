# Handoff debugging

Inspect without starting the capture dispatcher:

```text
recall-admin handoff list --user-id U --workspace-id W --project-id P --repository-id R --task-id T --session-id S
recall-admin handoff inspect HANDOFF_ID --user-id U --workspace-id W --project-id P --repository-id R --task-id T --session-id S --agent-id AGENT
recall-admin handoff explain HANDOFF_ID --user-id U --workspace-id W --project-id P --repository-id R --task-id T --session-id S --agent-id AGENT
```

Use `versions` to audit immutable snapshots. A blocked handoff may be returned
to `in_progress` with `retry`; source/requester may `cancel`; an authorized
participant may explicitly `expire` obsolete work. All mutations accept an
idempotency key.

Readiness exposes migration/service availability, policy version, counts for
in-progress, blocked, and expired handoffs, and oldest pending age. Prometheus
metrics contain counts and latency only—never project, task, agent, path, or
content labels. `last_error` reports sanitized service-stage failures.

If completion succeeded but usage telemetry failed, the canonical completion
and session checkpoint remain valid and the response carries a warning. Retry
feedback explicitly; do not resubmit a different completion under the same
handoff version.

Rollback follows the database-backup/prior-artifact procedure. There are no
down migrations.
