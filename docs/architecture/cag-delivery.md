# CAG delivery

Phase 6 adds a client-aware delivery layer to the canonical Context Pack V2
service. Scope is resolved before any cache lookup. A request may report known
checkpoint, pack, skill, and handoff versions; Recall verifies every claim and
returns `full`, `delta`, `no_change`, `refresh_required`, or `fallback_full`.

`cag-v1` always returns authoritative versions. Cache metadata is disposable:
tasks, sessions, memories, skills, handoffs, and context packs remain canonical.
Cache or delta failures rebuild from canonical sources and never mark content
used. REST and MCP both call `CAGDeliveryService`.

The server stores scoped cache metadata and immutable delivery records in
SQLite, while a thread-safe size-bounded LRU holds hot parsed packs. LRU
eviction affects residency only. It does not change durable freshness.

## Deterministic benchmark

The checked-in Phase 6 fixture measured a 2,234-token complete safe baseline.
An unchanged repeat delivered an estimated 110 tokens, avoided 2,124, and had
a 0.950761 estimated savings ratio. A session-only update delivered 1,033.
Mandatory retention was 100%, scope leakage was zero, and the forced-corruption
fallback succeeded. These are centralized-estimator values for one deterministic
fixture, not provider billing or a universal production-savings claim.
