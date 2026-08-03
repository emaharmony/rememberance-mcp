# Provider-neutral Recall Skill contract

All clients identify the user, workspace, project, optional repository, agent,
skill ID, and exact version when relevant. Mutation calls carry an idempotency
key. REST and MCP call the same `SkillService`.

REST provides `/v2/skills`, proposal, exact-version get/list, approve, reject,
refresh, explain, evidence, and feedback routes. MCP exposes
`recall_skill_list`, `get`, `propose`, `approve`, `reject`, `refresh`, `explain`,
`evidence`, and `feedback`.

Structured JSON is canonical. Markdown is a renderer over the same object for
Claude Code, Codex, and OpenClaw. MCP resources may reference the same immutable
representation; no client-specific truth store exists.

Context Pack V2 skill entries contain ID, version, approved status, delivery,
token estimate, expandability, content hash, and source fingerprint. Reference
expansion returns supporting evidence and records `expanded`. Clients must
explicitly report `used`, `ignored`, `corrected`, or `rejected`; inclusion alone
does not count as use.

Errors use the existing structured codes: missing/invalid input, `not_found`
for scope-safe hiding, `scope_mismatch` for invalid source scope,
`idempotency_conflict`, `forbidden`, and `invalid_transition`.
