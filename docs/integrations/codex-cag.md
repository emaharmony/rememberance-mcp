# Codex CAG integration

Codex uses the same provider-neutral state contract. A new workspace or target
agent sends no known state and receives full scoped context. Later turns report
the authoritative state from the prior response and accept `no_change`, compact
session, skill, or handoff deltas, or a safe full fallback. Never reuse state
across repositories or agents. Reference expansion and feedback remain explicit.
