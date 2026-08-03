# Client-state contract

`client_state` is optional and provider-neutral. It accepts `client_id`,
`client_type`, `known_checkpoint_version`, `known_context_pack_id`, a client
fingerprint hint, `known_skills`, `known_handoffs`, and extensible capabilities.
Missing state requests a full delivery.

Recall treats reported state only as a claim. It resolves user, workspace,
project, repository, task, session, and agent scope first, then verifies every
referenced artifact. Unknown or cross-scope identifiers return the same scoped
not-found behavior and reveal no existence. Useful capabilities include
`context_delta`, `skill_delta`, `handoff_delta`, `skill_reference`, and
`reference_expansion`.

Responses contain a delivery ID, mode, authoritative state, context and version
changes, warnings, estimated token accounting, and cache diagnostics.
