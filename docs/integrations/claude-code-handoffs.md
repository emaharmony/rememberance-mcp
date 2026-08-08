# Claude Code handoff adapter

Claude Code should identify itself as `claude-code`, persist the task/session
IDs, checkpoint before transfer, and create a handoff with the target agent and
expected output. It may render `content_markdown` directly. It should retain
the returned handoff and checkpoint versions, then request a compact delta
after target completion.

Hook adapters translate hook payloads into this shared contract; hook formats
are not canonical storage. A hook failure must not rewrite or delete a durable
handoff. The historical stop-hook failure has not been reproduced and no new
hook integration is introduced in Phase 5.
