# Utility Shadow Mode

Shadow mode is the Phase 2 default. Existing production ordering remains the
returned ordering while Recall records a proposed ordering using a capped 10%
utility contribution. The report includes current and proposed rank, direction,
score, policy version, lifecycle proposal, and component reasons; it never
includes captured content.

Inspect it with:

```text
recall-admin utility shadow-report
recall-admin utility inspect <memory-id>
```

Lifecycle recovery is explicit and audited:

```text
recall-admin utility cold <memory-id> --reason "stale"
recall-admin utility restore <memory-id>
recall-admin utility pin <memory-id>
recall-admin utility unpin <memory-id>
```

REST provides equivalent utility inspection and lifecycle operations under
`/v2/memories/{memory_id}/utility`; the deterministic report is available at
`/v2/admin/utility/shadow-report`.

`RECALL_UTILITY_SHADOW_MODE=true` and
`RECALL_UTILITY_RANKING_WEIGHT=0` are the safe defaults. An operator may disable
shadow mode and choose a weight from 0 through 0.1 only after evaluating the
same workload. Setting the weight back to zero is the immediate rollback.

The bundled deterministic benchmark contains relevant, duplicate, stale,
corrected/contradictory, cross-project, reused, and pinned examples. At K=3,
baseline precision and recall are 1/3, the proposed 10% shadow ordering is 2/3,
MRR remains 1.0, and cross-project leakage remains zero. This fixture is a
regression check, not a substitute for production evaluation.
