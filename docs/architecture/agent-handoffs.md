# Cross-agent handoffs

Recall handoffs are immutable, task-specific transfer snapshots. They are not
reusable Recall Skills and they do not duplicate canonical task state. A
handoff combines the current task, a compact session checkpoint/delta, approved
skill versions, and Context Pack V2 evidence for one source agent and one target
agent.

Construction is scope-first: user, workspace, project, repository, task, and
session relationships are validated before Context Pack V2 retrieval or skill
selection. Only the source, requester, and assigned target may read a handoff.
Only the target may claim, report progress, block, or complete it.

The priority order is objective, expected output, critical constraints,
approved decisions, blockers, work state, session delta, approved fresh skills,
important files, known failures, open questions, and evidence references.
This keeps the requested deliverable explicit and avoids transcript replay.

Every version stores structured JSON, full Markdown, compact Markdown, an exact
Context Pack V2 ID, a content hash, a source fingerprint, token estimate, policy
version, and expiry. Database triggers reject version mutation and deletion.
Terminal handoffs may be refreshed into a new immutable version; the prior
version remains readable and receives a `handoff.superseded` audit event.

Completion is a structured report. Recall appends an attributed
`handoff.completed` session event and creates a new checkpoint. New decisions are
stored as proposed, never approved implicitly. Actual reported skill and memory
use feeds Phase 2 telemetry; mere inclusion does not count as use.

Phase 5 does not add orchestration, multi-target claims, or NATS lifecycle
events. Phase 6 reuses immutable handoff versions for compact verified deltas.
