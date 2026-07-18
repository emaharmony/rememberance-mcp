# Upgrade to Recall 2.1

This release preserves the 2.0 MCP tools, REST compatibility routes, database location, memory IDs, legacy `remembrance_mcp` imports, commands, and `REMEMBRANCE_*` environment fallbacks.

## Before upgrading

1. Stop capture publishers or record the JetStream pending count.
2. Run `recall-admin integrity-check`.
3. Run `recall-admin backup BACKUP_DIR --include-nats` when unconsumed events must survive rollback.
4. Copy the backup, deployment configuration, secrets, current wheel/image, and its SHA-256 digest off-host.
5. Record the current Ollama models and NATS/Recall versions.

## Upgrade

Install the verified 2.1 artifact, run `recall-admin migrate`, pull `embeddinggemma`, run `recall-admin nats bootstrap`, and restart Recall. The migrations are additive: scope, processing, embedding provenance, raw-capture, and ingestion-idempotency data are added without renaming existing tables.

Check:

```text
recall-admin doctor
recall-admin integrity-check
recall-admin reembed --dry-run
GET /health/ready
GET /metrics
```

Then run `recall-admin reembed` if the embedding model or content hashes changed. Validate one known keyword search, one balanced search, project/agent filtering, capture, deletion, and a Prism event before reopening publishers.

## Rollback

A code-only rollback is permitted only when no 2.1-specific writes need to be retained. The safe rollback is:

1. Stop Recall and publishers.
2. Preserve the failed 2.1 state for diagnosis.
3. Restore the pre-upgrade SQLite backup and, if used, the matching JetStream snapshot.
4. Restore the previous artifact and deployment configuration.
5. Start dependencies and the previous Recall service.
6. Run its health check and validate a known search/capture.

Do not run two Recall writers against one database. Do not restore a JetStream snapshot into an existing stream; remove the failed stream only after preserving its state and confirming the matching backup.
