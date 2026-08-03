# Cache-key design

`CAGDeliveryService.cache_key` is the only cache-key builder. It canonicalizes
and hashes scope IDs, branch and commit, task/session state, token budget,
requested sections, delivery-affecting capabilities, selected immutable skill
and handoff versions, and policy versions.

Mapping and list order are normalized where order has no semantic meaning. The
key is a SHA-256 identifier and contains no objective, task text, memory text,
repository path, or other content. Changing scope, budget, checkpoint, policy,
branch, commit, or required version creates a different key.
