# Operations and monitoring

## Health

- `/health/live`: unauthenticated process liveness.
- `/health/ready`: loopback-only database, FTS, and configured NATS readiness.
- `/health` and `/v1/health`: compatibility responses.
- `/metrics`: authenticated Prometheus text metrics.

Alert when readiness fails for five minutes, disk free space drops below 20%, the latest successful off-host backup is older than 26 hours, NATS dead-letter count increases, outbox dead jobs appear, the oldest due outbox age grows continuously, or pending/failed embeddings grow continuously.

## Logs

Service logs must be collected off-host. Avoid logging bearer tokens, NATS passwords, raw authorization headers, or complete customer payloads. Windows WinSW logs roll by size/time; container deployments should use the platform logging driver with retention.

## Routine schedule

- Daily: online SQLite backup and encrypted off-host replication.
- Weekly: database integrity check and NATS JetStream snapshot.
- Monthly: restore the full stack onto a clean host and verify readiness/search.
- Before every upgrade: backup, integrity check, and record current artifact versions.
- After model changes: run `recall-admin reembed --dry-run`, then `recall-admin reembed`.

Recommended retention is 7 daily, 4 weekly, and 6 monthly backups. Model files are reproducible and are excluded from backups.

## Schema migrations

Take and verify a backup before upgrades, then run `recall-admin migrate`.
The command reports the database path, starting and resulting schema versions,
and every migration applied. A repeated run reports an empty `applied` list.

Recall fails startup for unknown future or non-contiguous migration histories.
Rollback restores the matching database backup and previous artifact. See
[Schema migrations](schema-migrations.md).

## Transactional outbox

`recall-admin outbox status` reports counts, active leases, and the oldest due
age without printing captured content. A dead job is terminal until an
operator runs `recall-admin outbox retry <job-id>`; the running Recall service
then picks up the reset job on its next poll.

Outbox counts always include `pending`, `processing`, `retry`, `complete`, and
`dead`, including zero values. `/stats` and `/health/ready` also report the
oldest due age, active leases, worker liveness, in-flight work, and the last
sanitized dispatcher error. Prometheus exports the same numeric queue state
plus `recall_outbox_dispatcher_error` as a content-free `0|1` gauge. The
one-shot `recall-admin doctor` command does not start or attach to a worker, so
it reports queue state and marks worker liveness/error as not observed.

Defaults are a 0.25-second poll interval, a 300-second lease, 10 attempts, and
2-second exponential retry bounded at 300 seconds. Override them with
`RECALL_OUTBOX_POLL_INTERVAL`, `RECALL_OUTBOX_LEASE_SECONDS`,
`RECALL_OUTBOX_MAX_ATTEMPTS`, and `RECALL_OUTBOX_RETRY_BASE_SECONDS`.

Stop Recall gracefully so in-flight work can finish. After an unclean exit,
wait for the old lease to expire or use the documented backup/restore path;
do not edit outbox rows manually.

Persisted processing errors contain only the exception type and the generic
message `capture processing failed`. Consult protected service diagnostics for
job IDs and retry state; raw exception messages and captured content are never
written to outbox health or operator-facing status surfaces.
