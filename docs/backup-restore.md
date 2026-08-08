# Backup and disaster recovery

`recall-admin backup [DESTINATION]` uses SQLite's online backup API for `memory.db` and `metrics.db`, writes SHA-256 checksums, and records a versioned manifest. Add `--include-nats` when the NATS CLI is installed to snapshot the file-backed stream and durable consumer.

Create an age-encrypted off-host artifact directly:

```text
recall-admin backup /secure/staging/recall --include-nats --age-recipient AGE_PUBLIC_KEY
```

Copy the resulting `.tar.gz.age` file off-host. Restore it with `recall-admin restore FILE.age --age-identity AGE_IDENTITY_FILE --include-nats --force`. Recall validates archive paths, rejects links, verifies every manifest checksum, and runs database integrity/foreign-key/orphan checks.

A complete disaster-recovery set contains:

- Recall and metrics SQLite backups plus manifest;
- deployment configuration and token/NATS credentials from a secrets manager;
- a NATS JetStream snapshot when unconsumed or replayable events matter;
- the exact Recall release artifact and deployment files.

To restore, stop Recall and publishers, place configuration and secrets, ensure the target JetStream stream does not exist when restoring a snapshot, run the restore command, run `recall-admin integrity-check`, start Ollama/NATS/Recall, and verify `/health/ready` and a known search query.

Never restore over a running Recall process. Never treat an untested backup as recoverable.
