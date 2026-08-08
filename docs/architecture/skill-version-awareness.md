# Skill-version awareness

Clients report optional `known_skills` as skill-ID-to-version mappings. Recall
looks up only fresh approved skills inside the resolved scope and classifies
them as unchanged, updated, missing, removed, or stale at the client.

Unchanged skills are acknowledged by ID and version only. Updated skills use a
deterministic structured delta when the schema, trust classification, and
configured size threshold permit it; otherwise the immutable new version is
sent in full. Pending, rejected, stale, and cross-scope skills are never exposed
as approved truth. Historical versions are never modified.
