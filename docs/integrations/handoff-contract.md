# Provider-neutral handoff contract

Clients identify `agent_id`, full task/session scope, known checkpoint version,
known handoff version, and an idempotency key. The canonical response is
provider-neutral JSON; Markdown and compact Markdown are renderings of the same
immutable object.

Creation requires source and target identities, requester, expected output,
capabilities, scope, and optional token budget. The returned handoff includes:

- task objective, status, and expected output;
- compact checkpoint and ordered session delta;
- completed/remaining work, blockers, questions, important files, failures;
- approved decisions and critical constraints;
- exact approved fresh skill versions;
- expandable evidence references;
- completion-report requirements, hashes, fingerprint, expiry, and policy.

The target calls `claim`, optionally `progress` or `block`, then `complete` with
files changed, work completed, tests, blockers, remaining work, proposed
decisions, questions, used skill versions, used memory IDs, and expanded
references. The source calls `delta` with its known versions and receives only
events, completion fields, and the resulting session delta.

Errors use the shared application error codes: `not_found` hides unauthorized
or cross-scope objects, `scope_mismatch` rejects invalid relationships,
`conflict` rejects lifecycle races, and `idempotency_conflict` rejects changed
retry input. Clients must not infer authorization from identifier knowledge.
