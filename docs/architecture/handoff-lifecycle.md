# Handoff lifecycle

The canonical states are:

```text
draft -> ready -> claimed -> in_progress -> completed
                    |            |
                    +-> blocked -+
```

Alternative terminal states are `rejected`, `expired`, `cancelled`, and
`superseded`. The service validates transitions under `BEGIN IMMEDIATE`, so two
agents cannot successfully claim one handoff. A repeated transition with the
same idempotency key returns the existing event; reuse with different input is
rejected.

Lifecycle events are append-only and exact-version scoped:
`handoff.created`, `handoff.ready`, `handoff.claimed`, `handoff.started`,
`handoff.progress`, `handoff.blocked`, `handoff.completed`,
`handoff.rejected`, `handoff.expired`, `handoff.cancelled`, and
`handoff.superseded`.

Claim joins the assigned target to the shared session as an implementer when
needed. Blocking emits a session blocker. Completion creates a session event
and deterministic checkpoint before marking the handoff complete. These
session writes are idempotent, so a retry repairs an interrupted completion
without duplicating canonical history.

Expiration or cancellation never deletes a version, event, completion, skill,
memory, or evidence reference. Retention cleanup remains deferred.
