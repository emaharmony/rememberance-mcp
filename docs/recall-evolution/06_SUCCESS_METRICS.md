# Recall Success Metrics

Recall succeeds when it helps agents continue work correctly, efficiently, and safely.

## 1. Cross-agent continuity

Primary metric:

```text
Percentage of handoffs completed without the user restating context
```

Supporting metrics:

- Handoff completion rate
- Missing-context incidents
- User clarification count after handoff
- Skill expansion requests
- Handoff failure reasons
- Resume accuracy

## 2. Repeated-explanation reduction

Measure:

- Repeated architecture explanations
- Repeated project constraints
- Repeated setup instructions
- Repeated decisions
- Corrections caused by lost context

## 3. Retrieval quality

Track:

```text
Precision@K
Recall@K
MRR
nDCG
Useful-context selection rate
```

Also track:

```text
Retrieved
Selected
Injected
Used
Helped task
Ignored
Corrected
Caused regression
```

## 4. Token efficiency

Measure:

- Tokens saved through cached skills
- Tokens saved through deltas
- Tokens avoided by not resending unchanged context
- Average context size
- Skill expansion rate
- Cache hit rate
- Cache invalidation rate

## 5. Task completion

Measure:

- Task completion rate with Recall
- Task completion rate without Recall
- Time required to resume work
- Repeated failed approaches
- Tasks completed across multiple agents

## 6. Factual consistency

Measure:

- Contradictions detected
- Contradictions resolved before skill promotion
- User corrections
- Stale skill incidents
- Unsupported claims
- Skills invalidated after source changes

## 7. Memory quality

Measure:

- Promotion rate
- Demotion rate
- Cold-memory reactivation rate
- Duplicate-detection accuracy
- Archived-memory recovery rate
- Incorrect purge incidents
- User disagreement with Curator recommendations

## 8. Reliability

Hard requirements:

```text
Acknowledged-but-lost canonical writes: zero
Cross-user leakage: zero
Cross-project leakage: zero
```

Also require:

- Recoverable derived-data failures
- Repeatable migrations
- Tested backup and restore
- Idempotent retries
- Observable enrichment backlog

## 9. Skill quality

Measure:

- Skill reuse across sessions
- Skill reuse across agents
- Skill correction rate
- Skill invalidation accuracy
- Average token savings per skill
- Skill expansion frequency
- Skill contribution to successful tasks

## 10. Curator quality

Measure:

- Correct duplicate suggestions
- Correct contradiction detections
- Accepted skill suggestions
- Rejected skill suggestions
- Correct demotions
- Incorrect demotions
- User validation burden

## 11. MVP pass criteria

The MVP is proven only when:

1. Claude Code establishes a task and session.
2. Recall compiles a handoff for Codex.
3. Codex loads the relevant skills.
4. Codex continues without the user repeating key context.
5. Codex returns a structured completion delta.
6. Claude Code resumes from the delta.
7. Recall records what context was selected and used.
8. The process uses less context than a full transcript resend.
9. Approved decisions remain authoritative.
10. No scope leakage occurs.
11. Stale sources are detectable.
12. Low-value memories can be demoted without immediate deletion.
