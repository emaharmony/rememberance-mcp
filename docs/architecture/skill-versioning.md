# Skill versioning and freshness

Skill IDs are stable while version rows are immutable and monotonically
allocated inside `BEGIN IMMEDIATE`. Database triggers reject updates or deletes
of `skill_versions` and `skill_sources`; reviews are append-only. Repeated
identical proposals return the existing version. A content or source change
creates the next candidate version.

Approval refers to one exact version. A newer candidate never inherits approval
and does not replace `current_approved_version` until reviewed. Historical,
rejected, and stale versions remain readable for audit.

`content_hash` covers canonical structured content. `source_fingerprint` covers
the schema, compiler policy, and ordered source identities, versions, hashes,
and relationships. A changed source marks the aggregate stale and creates or
permits a refreshed candidate; it never rewrites the approved snapshot.

`expires_at`, `retention_review_at`, and `stale_at` are separate. Expiration is
a review signal. Stale means current evidence no longer matches the compiled
view. Stale skills are excluded from default Context Pack injection and produce
a warning; direct scoped audit access remains available.

Rollback is forward-only: restore the pre-migration database backup and the
matching older artifact. Never run an older artifact against migration 9.
