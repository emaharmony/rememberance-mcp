# Prism compatibility and Context Pack V2

Prism's existing `POST /v1/context/build` contract remains supported. Recall
builds Context Pack V2 internally and adapts it to the established V1 fields:
`context_markdown`, `selected_memories`, `context_json`, `token_count`, and
`warnings`.

The adapter also returns additive fields older structs may ignore:
`context_pack_id`, `schema_version`, `checkpoint_version`,
`validation_requests`, `references`, `source_fingerprint`, and
`retrieval_run_id`.

Prism does not need to adopt V2 immediately. The V1 route accepts legacy
project and agent vocabulary and retains its status and response shape. It no
longer marks every candidate injected; the shared allocator records only
inline and summary evidence as injected.

A future Prism adapter can call `/v2/context/build`, expand references, and
submit scoped feedback directly. No external Prism repository modification is
included in Phase 3; that remains a client-side follow-up.
