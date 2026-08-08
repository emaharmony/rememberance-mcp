# Prism handoff adapter

Prism remains the workflow orchestrator; Recall owns context and continuity.
A Prism adapter should create, fetch, claim, complete, and retrieve deltas via
the REST or MCP handoff contract. The canonical JSON representation is suitable
for Prism; exact skill and reference expansion uses the same Recall scope.

This repository does not contain an independently deployable Prism client
change. The existing V1 Prism context endpoint remains compatible. External
Prism integration is a documented follow-up, not a partial cross-repository
mutation.
