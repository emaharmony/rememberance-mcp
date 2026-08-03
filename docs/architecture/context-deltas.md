# Context deltas

Context deltas compare a verified client Context Pack V2 with the current
authoritative pack. They cover task state, compact session checkpoint changes,
decisions, constraints, blockers, questions, validation requests, references,
skills, and warnings. They never replay the complete transcript.

An unavailable pack, future checkpoint, fingerprint mismatch, policy mismatch,
corrupt artifact, or failed comparison produces a complete safe fallback.
When nothing changed, `no_change` still returns the current pack fingerprint,
checkpoint, skill versions, and handoff versions.

If the estimated delta is not smaller than the full safe context, Recall sends
the full pack. Mandatory context remains governed by Context Pack V2 and cannot
be counted as token savings by omission.
