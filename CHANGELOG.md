# Changelog

## 2.1.0 - Unreleased

### Added

- Environment-backed validated production settings and secure non-loopback startup checks.
- Bearer authentication, token rotation overlap, request/rate limits, exact CORS, readiness, metrics, context POST, and memory deletion.
- Ollama embedding provider, embedding provenance, real vector/balanced retrieval, scoped search, and stale-vector repair.
- Raw-first capture processing, structured fact extraction, access counters, ingestion event IDs, foreign-key cleanup, and operational statistics.
- Durable NATS JetStream consumer with explicit post-commit acknowledgements, retries, and dead-letter handling.
- `recall-admin` initialization, doctor, migration, model, NATS, token, backup, restore, integrity, re-embedding, and version commands.
- Manifest-verified backup/restore with optional age encryption and JetStream snapshots.
- Automated contradiction resolution, provenance-bearing fact reads, and deep entity query expansion.
- Ubuntu Compose and Windows native service deployment assets.
- Architecture, security, operations, monitoring, backup, deployment, API, and configuration documentation.
- Cross-platform CI plus an explicit coverage, security, SBOM, and artifact release gate.
- Reproducibility-checked, SBOM-attached, keyless Sigstore-signed release wheels.

### Fixed

- Preference category validation.
- Vector-mode positional fallback.
- Immediate active-memory promotion.
- Balanced search omitting vectors.
- Graph-only scores outranking direct RRF results.
- Ignored project/agent scopes.
- FactStore being disconnected from capture.
- REST route/documentation mismatch and raw exception disclosure.

### Compatibility

Legacy Remembrance imports, commands, environment variables, health routes, MCP tools, table names, and memory IDs remain supported during the 2.1 migration window.
