# Session debugging

Use the shared REST or MCP service rather than editing SQLite rows. Session
events are append-only and direct update/delete statements intentionally fail.

## Inspection

1. Read `GET /v2/tasks/{task_id}` and confirm the user, workspace, project, and
   repository IDs.
2. Read `GET /v2/sessions/{session_id}` and confirm the task, participants,
   status, current checkpoint version, and source agent.
3. Read `GET /v2/sessions/{session_id}/delta?known_version=0` to inspect all
   retained events in deterministic sequence order.
4. Repeat with the client's known version to reproduce its handoff view.

Equivalent MCP tools are `recall_task_get` and `recall_session_delta`.

## Common errors

- `scope_mismatch`: an existing repository/project ID was presented under a
  different parent, or a session was paired with another task.
- `idempotency_conflict`: a retry key was reused with different canonical
  input. Generate a new key only for a genuinely new operation.
- `invalid_version`: the client claims a checkpoint newer than the session.
- `version_too_old`: the referenced checkpoint is no longer retained. Phase 1
  does not prune checkpoints, but clients must handle this future-safe error.
- `not_found`: the record does not exist or is outside the requested scope.

Closing a session is reversible only by starting another session for the task;
closed session history remains readable. There is no down migration. Rollback
requires a pre-upgrade database backup and the matching prior artifact.

