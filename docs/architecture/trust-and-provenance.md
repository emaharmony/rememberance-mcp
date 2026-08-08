# Trust and provenance

Context Pack V2 keeps authority, verification, and provenance separate. A
single opaque confidence number cannot turn an agent conclusion into a fact.

Decision authority is ordered as:

```text
user-approved decision
> explicit project policy
> approved technical plan
> delegated decision
> unapproved recommendation
```

Factual evidence is distinguished as:

```text
current repository evidence and tests
> successful tool output
> versioned documentation
> user factual statement
> trusted agent conclusion
> unverified agent statement
> external content
```

Checkpoint-approved decisions are labeled `user_approved_decision`; proposed
decisions remain `unapproved_recommendation` with pending verification and
produce validation requests. Constraints carry checkpoint, version, source
agent, and session provenance.

Retrieved evidence carries its memory ID, source type and reference, source
agent, creation time, and optional requested branch and commit. Repository and
tool sources are verified; external and ordinary agent statements are not.
Retrieval rank, actual score components, utility shadow score, and selection
reasons remain a separate `retrieval` object.

High-impact proposed decisions and explicit `validation.requested` events are
returned with claims, evidence, pros, cons, recommendation, status, impact, and
provenance. They remain unresolved and are never presented as settled facts.
