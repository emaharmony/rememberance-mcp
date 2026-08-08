# Prism CAG integration

Prism's existing `/v1/context/build` response remains unchanged. A future Prism
adapter may opt into `/v2/context/deliver`, store authoritative versions per
agent workflow, and forward structured deltas. Recall remains the source of
context truth; Prism remains the orchestrator. No external Prism repository was
modified in Phase 6.
