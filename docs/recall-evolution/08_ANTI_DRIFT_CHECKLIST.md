# Recall Anti-Drift Checklist

Use this checklist before approving a new Recall feature or roadmap change.

## Product alignment

- Does this improve cross-agent continuity?
- Does this reduce repeated explanations?
- Does this improve retrieval quality?
- Does this reduce token usage?
- Does this improve task completion?
- Does this improve factual consistency?

If the answer is no to all six, the feature is likely outside the current plan.

## Current-phase discipline

- Is this required by the earliest incomplete roadmap phase?
- Does the current phase have a clear exit gate?
- Has the current phase demonstrated measurable value?
- Are we skipping reliability work for a more exciting feature?
- Can this be implemented as a smaller coherent slice?

## Scope discipline

- Is this needed for Prism, Claude Code, or Codex now?
- Is this required for the first handoff workflow?
- Is this solving a measured problem or a hypothetical future problem?
- Does it introduce a new service or operational dependency?
- Is the new dependency justified by measured need?

## Reliability

- Is canonical data durable before acknowledgment?
- Can derived data be rebuilt?
- Are failures visible?
- Is the operation idempotent?
- Are migrations formal and tested?
- Are backup and restore covered?
- Are foreign keys and constraints enforced?

## Retrieval quality

- Is retrieval scoped before ranking?
- Is output backed by evidence?
- Is usefulness measured?
- Can Recall explain why an item was selected?
- Does the feature improve a benchmark or outcome metric?

## Skill safety

- Does the skill reference evidence?
- Does it store source hashes?
- Is it versioned?
- Can it be invalidated?
- Can the client expand it to evidence?
- Could it become stale after repository changes?
- Does it distinguish facts from recommendations?

## Trust and approval

- Is this a fact, decision, recommendation, hypothesis, external claim, or tool result?
- Is the source type recorded?
- Is verification recorded?
- Does an important contradiction require user approval?
- Could an agent statement become canonical without validation?

## Retention

- Is low-value content suppressed before deletion?
- Is the reason for demotion recorded?
- Can archived content be recovered?
- Are pinned and approved records protected?
- Is expiry extended only after meaningful use?
- Could automatic pruning erase rare but important history?

## Caching

- What is the exact cache key?
- What invalidates the cache?
- Are branch and commit relevant?
- Are skill versions included?
- Could stale cache output cause a confident mistake?
- Is Redis actually needed?
- Can local caching prove the design first?

## Curator boundaries

- Is the Curator recommending or deciding?
- Could it override the user?
- Could it promote external content without verification?
- Could it purge pinned memory?
- Could it modify repositories?
- Are deterministic rules controlling its authority?

## Architecture boundaries

- Is Recall remaining the context layer?
- Is Prism remaining the workflow orchestrator?
- Is transport-specific business logic being duplicated?
- Is authoritative data separated from derived data?
- Is a new abstraction replacing an existing one unnecessarily?

## Phase gate

Do not begin the next phase until the current phase has:

- Passing tests
- Measured usefulness
- Documented failure modes
- A rollback path
- User-visible value
- No unresolved critical scope issue

## Stop signs

Pause implementation if:

- Retrieval remains unreliable
- Context packs are mostly noise
- Users repeatedly correct skills
- Handoffs still require restating the task
- Scope leakage is observed
- Cache invalidation rules are unclear
- New infrastructure is added without measured need
- Automatic pruning removes information later needed
- The Curator is making unapproved truth decisions
- Several roadmap phases are being built at once

## North Star reminder

> **Build reliable, evidence-backed, versioned continuity between heterogeneous AI agents.**

That is the product.

Everything else is supporting infrastructure.
