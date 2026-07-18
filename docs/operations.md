# Operations and monitoring

## Health

- `/health/live`: unauthenticated process liveness.
- `/health/ready`: loopback-only database, FTS, and configured NATS readiness.
- `/health` and `/v1/health`: compatibility responses.
- `/metrics`: authenticated Prometheus text metrics.

Alert when readiness fails for five minutes, disk free space drops below 20%, the latest successful off-host backup is older than 26 hours, NATS dead-letter count increases, or pending/failed embeddings grow continuously.

## Logs

Service logs must be collected off-host. Avoid logging bearer tokens, NATS passwords, raw authorization headers, or complete customer payloads. Windows WinSW logs roll by size/time; container deployments should use the platform logging driver with retention.

## Routine schedule

- Daily: online SQLite backup and encrypted off-host replication.
- Weekly: database integrity check and NATS JetStream snapshot.
- Monthly: restore the full stack onto a clean host and verify readiness/search.
- Before every upgrade: backup, integrity check, and record current artifact versions.
- After model changes: run `recall-admin reembed --dry-run`, then `recall-admin reembed`.

Recommended retention is 7 daily, 4 weekly, and 6 monthly backups. Model files are reproducible and are excluded from backups.
