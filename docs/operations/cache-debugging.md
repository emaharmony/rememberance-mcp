# Cache debugging

Use content-free diagnostics without starting the capture worker:

```text
recall-admin cache status
recall-admin cache list --user-id U --workspace-id W --project-id P --repository-id R --task-id T --session-id S --agent-id A
recall-admin cache inspect CACHE_ID --user-id U --workspace-id W --project-id P --repository-id R --task-id T --session-id S --agent-id A
```

Inspect status, source fingerprint, policy, expiry, invalidation reason, access
count, and dependency hashes. Diagnostics do not print cached context content.
`recall-admin delivery explain` reports why a delivery mode was selected.
