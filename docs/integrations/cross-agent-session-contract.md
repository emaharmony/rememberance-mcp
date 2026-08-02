# Cross-agent session contract

This contract is provider-neutral. Claude Code, Codex, Prism, OpenClaw, and
future adapters translate their native events into the same application calls.

## Required identity

Task creation requires `user_id`, `workspace_id`, `project_id`, `repository_id`,
`title`, `objective`, and the creating `agent_id`. A client may also provide an
extensible `agent_system_type`, repository path/remote/branch metadata, and an
`idempotency_key`.

Every session write carries `session_id`, `agent_id`, and an idempotency key
when it may be retried. An agent joins once with a role. The task belongs to the
user/project, not the initiating agent.

## Checkpoint shape

```json
{
  "session_id": "session_...",
  "agent_id": "codex",
  "summary": "Phase 1 service complete",
  "completed": ["schema", "service"],
  "remaining": ["API"],
  "constraints": ["do not push"],
  "approved_decisions": ["structured checkpoints"],
  "proposed_decisions": [],
  "open_questions": [],
  "blockers": [],
  "important_files": ["src/recall_mcp/continuity.py"],
  "known_failures": [],
  "idempotency_key": "client-stable-operation-id"
}
```

## Delta shape

A joining agent sends `session_id`, `agent_id`, and
`known_checkpoint_version`. Version zero requests all retained state. The
response contains `from_version`, `to_version`, ordered attributed `events`,
the latest newer `checkpoint`, `completed_added`, `remaining_changed`,
`new_decisions`, `new_blockers`, `new_open_questions`, and `no_change`.

Clients do not resend transcripts. They persist the returned `to_version`,
append their own events, create the next checkpoint, and hand the new version
back to the other agent.

## Transports

- REST: `/v2/tasks` and `/v2/sessions` routes.
- MCP: `recall_task_*` and `recall_session_*` tools. Successful tool bodies are
  JSON.
- Context: `/context/build`, `/v1/context/build`, and
  `memory_context_build` accept optional formal scope, task/session IDs, agent
  ID, and known checkpoint version. Legacy query-only calls remain valid.
- NATS: existing agent-output subjects remain unchanged. Optional formal scope,
  task, and session fields are forwarded to the same durable capture pipeline;
  acknowledgement still follows durable raw-capture/outbox insertion.

HTTP errors include a stable code and safe message. MCP invalid requests return
safe error text. Idempotent replays return the original resource with
`idempotent_replay=true`.

