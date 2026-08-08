# Recall Skills

A Recall Skill is a scope-aware compiled view over evidence. It is not a new
source of truth: every version retains immutable source rows and can be expanded
back to its memories, repository evidence, decisions, session events, tool
output, documentation, facts, or explicitly labeled external content.

The canonical object contains a stable skill ID, immutable integer version,
schema version, user/workspace/project/repository scope, purpose, summary,
instructions, facts, decisions, constraints, open questions, evidence metadata,
content hash, source fingerprint, compiler policy version, token estimate, and
JSON and Markdown renderings. Empty optional sections stay empty.

Candidate generation is deterministic. `SkillCandidateService` supports memory,
Context Pack, and repository-evidence entry points, all delegated to
`SkillService`. Scope is resolved before any source row is read. Frequent
retrieval alone is never sufficient; all candidates require explicit evidence
and manual review. LLM assistance may populate generated summaries in the
future, but cannot decide scope, trust, hashes, versions, staleness, or approval.

Context Pack V2 selects only fresh approved versions. Mandatory task state,
approved decisions, critical constraints, and blockers remain ahead of the
skill budget. Delivery is `inline`, `summary`, `reference`, or `omitted`; the
exact version and disposition are persisted in `context_pack_skills`.
Selection/injection/reference are recorded separately and never imply use.

Automatic publication, automatic approval, skill caching, and permanent
deletion are not part of Phase 4.
