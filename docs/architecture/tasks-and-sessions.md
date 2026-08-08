# Tasks and sessions

Recall models shared work independently of any one agent:

```text
User
└── Workspace
    └── Project
        └── Repository
            └── Task
                └── Session
                    ├── Claude Code
                    ├── Codex
                    ├── Prism
                    └── OpenClaw
```

A repository is read-only metadata in this phase. A task owns the objective and
lifecycle state. A session owns participants, an append-only event stream, and
monotonic checkpoints. Completing a task or closing a session never removes
history or memories.

## Transactions and identity

Task creation uses one `BEGIN IMMEDIATE` transaction to register the initiating
agent, validate or create the local scope records, and insert the task. An
existing workspace, project, or repository ID must match its original parent
scope. A repository cannot be reused under another project.

Session writes also use `BEGIN IMMEDIATE`. Every event receives a unique,
monotonic sequence within its session. Database triggers reject event updates
and deletes. Client idempotency keys are unique per task or session and reuse
with different canonical input is an error.

## Lifecycle

Task states are `planned`, `active`, `blocked`, `review`, `completed`, and
`cancelled`. Session states are `active` and `closed`. Participant roles are
`owner`, `orchestrator`, `implementer`, `reviewer`, and `observer`.

Supported event types are:

- `session.started`, `session.closed`
- `agent.joined`, `agent.left`
- `task.updated`, `work.completed`, `validation.requested`
- `checkpoint.created`
- `decision.proposed`, `decision.approved`
- `blocker.reported`

Agent conclusions remain attributed session events. They are not promoted to
facts automatically.

## Checkpoints and deltas

Checkpoints are deterministic structured state, not transcript summaries. Each
contains the task objective, summary, completed and remaining work, constraints,
approved and proposed decisions, open questions, blockers, important files,
known failures, repository/branch metadata, source agent, source event, and
whether generation was used. Version numbers start at one and increase by one.

`get_delta(session_id, known_version=N)` returns ordered events after the source
sequence of checkpoint N, the newest checkpoint when it is newer, and
deterministic list differences. Version zero means the beginning of retained
history. A version newer than the session is invalid. A missing retained
version returns `version_too_old`; Phase 1 retains all versions, so the current
retention floor is zero. An already-current request returns `no_change=true`
unless uncheckpointed events exist.

## Retrieval scope

Formal memory scope columns are nullable for legacy compatibility. When formal
scope is supplied, keyword, vector, and graph-augmentation queries constrain
SQLite rows before score calculation or fusion. Unscoped legacy calls retain
their previous behavior. Global graph synthesis is omitted from formally scoped
context until graph facts themselves carry the same formal scope.

