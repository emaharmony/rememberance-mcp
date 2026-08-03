# OpenClaw handoff adapter

OpenClaw should use its stable Recall agent ID, pass the complete task/session
scope and known versions, and select JSON, Markdown, compact, reference, and
delta capabilities explicitly. Workspace files may render the canonical
Markdown, but they are not a second source of truth.

The adapter must preserve exact skill versions, target assignment, and
idempotency keys. Missing references should be expanded through Recall rather
than reconstructed from another client's private transcript format.
