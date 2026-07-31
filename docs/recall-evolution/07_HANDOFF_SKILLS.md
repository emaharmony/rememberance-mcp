# Recall Handoff Skills

## Purpose

Handoff Skills let one agent transfer active work to another agent without sending the entire conversation or forcing the user to restate the task.

## Core flow

```text
Parent agent works
→ checkpoints the task
→ Recall compiles a handoff skill
→ target agent loads the handoff and missing skills
→ target agent performs work
→ target agent reports a structured result
→ Recall updates task and session state
→ parent agent receives only the delta
```

## Handoff structure

```yaml
id: handoff:task-123:codex
version: 4

scope:
  user_id: ema
  project_id: recall
  repository_id: emaharmony/recall
  task_id: task-123
  session_id: session-456

task:
  objective: Implement Recall configuration compatibility
  expected_output: Pull request with tests

current_progress:
  completed:
    - Canonical package renamed
    - Legacy import shim added
  remaining:
    - Rename scripts
    - Update environment variables

constraints:
  - Do not alter the database schema
  - Preserve legacy paths
  - Run clean-wheel installation tests

relevant_skills:
  - skill:recall:architecture@7
  - skill:recall:testing@3

important_files:
  - pyproject.toml
  - src/recall_mcp/config.py

known_failures:
  - Legacy warning previously wrote to stdout

open_questions:
  - Which warning mechanism is safest for MCP stdio?

validation_requirements:
  - User approval required before removing legacy support

source_agent: claude-code
target_agent: codex
created_at: ...
expires_at: ...
```

## Required sections

Every handoff should include:

- Objective
- Expected output
- Current progress
- Completed work
- Remaining work
- Constraints
- Relevant skills
- Important files
- Known failures
- Open questions
- Validation requirements
- Source agent
- Target agent
- Scope
- Version
- Expiration

## Parent-agent responsibilities

The parent agent should:

- Checkpoint the session
- Identify finished and unfinished work
- State constraints explicitly
- Attach relevant skills
- Include evidence references
- Avoid dumping the complete transcript
- State the expected result

## Target-agent responsibilities

The target agent should:

- Confirm the handoff version
- Load only missing skill versions
- Request expansion only when needed
- Preserve constraints
- Report changed files and outcomes
- Record blockers
- Return a structured delta

## Completion delta

```yaml
handoff_id: handoff:task-123:codex
from_version: 4
status: completed

changes:
  completed:
    - Added environment compatibility helper
    - Added precedence tests
  remaining:
    - Update Windows wrappers

files_changed:
  - src/recall_mcp/config.py
  - tests/test_config_compat.py

tests:
  passed: 218
  failed: 0

new_decisions:
  - Legacy warnings use logging to stderr

new_blockers: []
new_open_questions: []
recommended_next_action: Update Windows wrappers
```

## Version negotiation

Clients report known versions:

```json
{
  "known_skills": {
    "skill:recall:architecture": 6
  },
  "known_handoffs": {
    "handoff:task-123:codex": 3
  }
}
```

Recall returns only:

- Missing skills
- Updated versions
- Handoff deltas
- Newly relevant evidence

## First handoff benchmark

```text
Claude Code
→ Recall handoff
→ Codex implementation
→ Recall completion delta
→ Claude Code review
```

The user should not need to restate:

- The objective
- Repository
- Constraints
- Decisions
- Completed work
- Important files
