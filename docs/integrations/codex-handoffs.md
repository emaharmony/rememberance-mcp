# Codex handoff adapter

Codex should identify itself as `codex`, fetch or claim only handoffs assigned
to it, and prefer structured JSON when available. It may use compact Markdown
for prompt delivery and expand exact skill/evidence references on demand.

Completion must report observed files and test results separately from proposed
decisions. Only skill versions and memories actually used should be reported as
used. Inclusion, reference delivery, and expansion are distinct telemetry
signals.
