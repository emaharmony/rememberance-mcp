# Stale skill recovery

When repository evidence, a source memory, an approved decision, a fact, a
dependency, or compiler policy changes, mark the skill stale and inspect its
exact historical version. Context Pack V2 excludes stale skills and falls back
to direct retrieval while surfacing `stale_skills_excluded`.

Recovery is reversible and audited:

1. Inspect the stale reason and expand old evidence.
2. Refresh from current in-scope sources.
3. Confirm that a new immutable candidate version was created.
4. Review contradictions and source hashes.
5. Approve the new exact version manually.

The old version remains available throughout. Do not edit it, delete it, or
clear staleness without evidence. If the refresh is wrong, reject the candidate
and keep direct evidence delivery enabled.
