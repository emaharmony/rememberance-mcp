# Cache invalidation operations

Invalidate one entry or a scoped project subtree:

```text
recall-admin cache invalidate CACHE_ID --user-id U --workspace-id W --project-id P --reason manual_review
recall-admin cache invalidate --user-id U --workspace-id W --project-id P --reason project_policy_changed
recall-admin cache prune-expired --user-id U --workspace-id W --project-id P
```

Invalidation is audited, removes matching hot entries, and preserves canonical
artifacts. Rebuild is lazy on the next request. `prune-expired` marks expired
metadata; it does not delete tasks, memories, packs, skills, or handoffs.
