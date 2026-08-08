# Context Pack V2

Context Pack V2 is Recall's canonical provider-neutral context-delivery model.
REST, MCP, Prism compatibility, and local callers all delegate to
`ContextPackService`; transports do not assemble or rank context themselves.

## Build flow

```text
validate request
-> resolve user/workspace/project/repository/task/session scope
-> load task and compact session state
-> calculate the requested checkpoint delta
-> collect approved decisions, constraints, blockers, and validation requests
-> retrieve within the permitted scope
-> attach trust, provenance, and Phase 2 score metadata
-> allocate the token budget
-> persist the pack, references, dispositions, and feedback linkage atomically
-> return the provider-neutral response
```

Scope predicates are passed into retrieval before ranking. A defensive scoped
read verifies every result before assembly; a mismatch fails the build rather
than filtering a ranked global result afterward.

## Contract

Every pack has `schema_version: 2` and a versioned `policy_version`. Its major
sections are `scope`, `request`, `active_task`, `session`, `decisions`,
`constraints`, `work_state`, `retrieval`, `inline_context`, `references`,
`validation_requests`, `warnings`, `omissions`, `token_usage`, and `freshness`.
Optional values are omitted or null; Recall does not manufacture branch,
commit, vector, graph, or confidence values that did not exist.

## Determinism and persistence

For the same source state and policy versions, selection order, dispositions,
reference IDs, token estimates, and `source_fingerprint` are stable. Pack IDs
and generation timestamps are new identities unless the caller supplies an
idempotency key. Reusing that key with the same request returns the original
pack; changing the request produces `idempotency_conflict`.

Migration 8 extends the Phase 2 `context_packs` table and adds normalized item
and reference rows. The full structured pack and a content-minimized
explanation are stored. Canonical memories and session history are never
rewritten during generation. Pack persistence, item dispositions, reference
storage, and selection/injection feedback commit in one SQLite transaction.

The source fingerprint covers stable task state, checkpoint state, evidence
IDs and content digests, branch and commit when supplied, and context,
estimator, and utility policy metadata. It excludes retrieval timestamps and
hidden model reasoning. Phase 3 exposes freshness metadata but does not
implement Context-Augmented Generation caching.

## Feedback lifecycle

Every included or omitted memory belongs to the pack's retrieval run or an
explicit mandatory section. Generation records candidates as `returned`,
inline/summary/reference items as `selected`, and only inline/summary items as
`injected`. References and omissions are never marked used. `expanded`, `used`,
`ignored`, `corrected`, and `rejected` require a client feedback action.

Utility remains shadow-only by default. Context Pack V2 reports utility
metadata and whether utility affected production rank; it does not silently
enable utility ranking.

`client_capabilities`, `requested_sections`, stable references, policy versions,
and fingerprints now carry optional Phase 4 approved skill versions. Skills use
their own budget class and never displace mandatory context. Pending or stale
versions are excluded, while exact selected versions and usage semantics remain
auditable. Phase 6 may cache and delta-deliver this immutable output, but it does
not automatically publish skills or change Context Pack V2 assembly rules.

## Deterministic benchmark

The checked-in K=3 evaluation set covers repository evidence, a near duplicate,
long tool output, stale and corrected memories, frequently reused context, a
contradictory claim, and a cross-project distractor. Current Phase 3 results are
100% mandatory retention, Precision@3 1.0, Recall@3 1.0, MRR 1.0, 490 estimated
tokens delivered, 700 deferred or omitted, one reference among three selected
items, and zero cross-project leakage. This measures context compression and
reference deferral only; it makes no claim about future Skill or CAG savings.

Known limitations are the approximate provider-neutral estimator, client-
reported use feedback, no external client adapter changes in this repository,
no automatic conversion of arbitrary memories into validation requests, no
new NATS context lifecycle subjects, shadow-only utility by default, and no
automatic skill approval. Phase 6 cache behavior is documented separately in
[`cag-delivery.md`](cag-delivery.md).
