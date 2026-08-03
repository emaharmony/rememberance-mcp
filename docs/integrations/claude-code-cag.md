# Claude Code CAG integration

Claude Code should persist only the last authoritative checkpoint, pack ID and
fingerprint, exact skill versions, and relevant handoff versions. Send them to
`POST /v2/context/deliver` with `context_delta`, `skill_delta`, and
`reference_expansion` capabilities. Apply explicit removals and refresh
instructions before deltas. Report actual usage through delivery feedback;
receipt alone is not use.

The Stop hook remains a capture adapter, not the canonical cache. See
[Stop-hook diagnostics](../operations/stop-hook-diagnostics.md).
