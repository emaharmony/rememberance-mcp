# Skill review operations

Use REST, MCP, or the no-worker admin surface to inspect candidate content and
evidence before approving an exact version.

```text
recall-admin skill list --user-id USER --workspace-id WS --project-id PROJECT --repository-id REPO
recall-admin skill inspect SKILL --version 1 --user-id USER --workspace-id WS --project-id PROJECT --repository-id REPO
recall-admin skill evidence SKILL --version 1 --user-id USER --workspace-id WS --project-id PROJECT --repository-id REPO
recall-admin skill approve SKILL --version 1 --reviewer-id USER --user-id USER --workspace-id WS --project-id PROJECT --repository-id REPO
```

Before approval, verify source hashes, contradiction relationships, decision
labels, critical constraints, compiler policy version, and token estimate.
Reject with a reason when evidence is incomplete. A changed candidate must be a
new version and must be reviewed again.

Readiness reports service/migration availability, pending approvals, stale
count, and compiler policy. Prometheus metrics expose counts only; they never
label skill title, user, project, repository, or evidence content.
