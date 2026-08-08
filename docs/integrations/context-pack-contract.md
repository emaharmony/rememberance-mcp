# Cross-provider Context Pack V2 contract

Claude Code, Codex, Prism, OpenClaw, and future clients use the same identity
contract. Adapters translate hook or transport shapes into this model; they do
not implement context policy.

## Build request

`POST /v2/context/build` and MCP `recall_context_build` accept:

```json
{
  "schema_version": 2,
  "user_id": "user",
  "workspace_id": "workspace",
  "project_id": "project",
  "repository_id": "repository",
  "task_id": "task_123",
  "session_id": "session_456",
  "agent_id": "codex",
  "objective": "optional query override",
  "max_tokens": 3000,
  "known_checkpoint_version": 4,
  "known_context_pack_id": null,
  "branch": "feature/context-v2",
  "commit_sha": "abc123",
  "requested_sections": [],
  "client_capabilities": ["structured_json", "references", "session_delta"],
  "idempotency_key": "provider-retry-key"
}
```

Formal V2 requires user, workspace, project, repository, task, and agent scope.
Session is optional; a checkpoint version requires a session. Supported
capabilities are `structured_json`, `markdown`, `references`, `session_delta`,
and `expandable_evidence`. Unknown capabilities produce a warning.

## Operations

REST provides build, scoped get, explain, reference expansion, and feedback at
`/v2/context`. MCP provides `recall_context_build`, `recall_context_get`,
`recall_context_explain`, `recall_context_expand_reference`, and
`recall_context_feedback`.

Fetch, explain, expansion, and feedback require matching formal scope. MCP
errors are JSON with `error` and `code`; REST uses the same code with 400, 404,
409, or 503. Idempotent replay returns the same pack and
`idempotent_replay: true`.

Clients submit `known_checkpoint_version`, not a transcript. With no version,
Recall returns the compact checkpoint. With a current version, delta status is
`current`. With an invalid version, Recall returns a safe checkpoint plus a
warning.

Building a pack is not evidence of use. Clients report expansion, use, ignore,
correction, and rejection explicitly. Successful outcomes use the existing
Phase 2 task-outcome contract.
