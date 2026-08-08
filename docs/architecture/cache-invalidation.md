# Cache invalidation

Each CAG cache entry records hashes for the task, session checkpoint, decisions,
constraints, validation state, selected memories, approved skill versions,
handoff versions, repository commit, context policy, CAG policy, and token
estimator. TTL is checked in addition to these dependencies, never instead of
them.

Changed dependencies mark entries stale or invalid, remove the matching hot
entry, append an invalidation audit record, and rebuild lazily. Expiration means
the configured cache lifetime elapsed; staleness means source state changed;
invalidation is an explicit or policy decision; eviction only removes hot
residency. Repeated access cannot make stale data fresh.
