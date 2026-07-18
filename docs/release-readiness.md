# Recall 2.1 release readiness

Status as of 2026-07-17: feature-complete release candidate; not yet release-certified.

## Score

| Dimension | Score | Evidence |
| --- | ---: | --- |
| Architecture and boundaries | 9.4 | Raw-first pipeline, typed configuration, bounded workers, durable delivery, exact-vector retrieval, and private exposure model |
| Correctness and data integrity | 9.1 | Regression fixes, migrations, foreign keys, cascades, idempotency, integrity/orphan checks, and verified backup restore |
| Security | 9.2 | Bearer auth, constant-time comparison, rotation overlap, fail-closed binding, limits, safe errors, artifact signing, zero high Bandit findings, and no known installed dependency vulnerabilities |
| Operations and deployment | 9.0 | Admin CLI, health/readiness/metrics/logging, Ubuntu Compose, Windows services, Tailscale runbooks, and disaster recovery |
| Automated assurance | 6.8 | 187 local tests pass, but total line coverage is 62% and the external matrix has not run |
| Release evidence | 7.0 | Package and deployment syntax are verified locally; OS certification, load, outage, restore, and soak evidence remain |

The current weighted production-readiness score is **8.7/10**. The implemented
architecture and feature set are above 9; the repository must pass the remaining
assurance gates below before the overall score can honestly exceed 9.

## Locally verified

- 187 tests pass and 2 environment-dependent tests skip.
- Ruff formatting and lint pass.
- Mypy passes for all 39 source files. The deprecated pre-2.1 NATS compatibility
  adapter has an explicit override; the durable production subscriber is checked.
- Bandit reports zero high-severity findings.
- pip-audit reports no known vulnerabilities in the installed production stack.
- The 2.1 wheel and source distribution build successfully; a clean target import
  reports version 2.1.0, and the source distribution includes Ubuntu and Windows
  deployment assets.
- All GitHub workflow and Compose YAML parses successfully.
- Docker Compose renders with a non-empty shared Recall/NATS credential.
- Both Windows PowerShell deployment scripts parse successfully.
- REST authentication, token overlap, request limits, raw-first asynchronous
  capture, semantic retrieval, fact resolution, migrations, cascades, backups,
  restore, and mocked JetStream acknowledgement/redelivery/DLQ behavior have
  focused regression coverage.

## Release blockers

1. Raise total line coverage from 62% to at least 90%, branch coverage to 85%,
   and the security/storage/capture/delivery group to 95%.
2. Run the live JetStream integration test against NATS. It is currently skipped
   because the local Docker daemon is unavailable.
3. Pass CI on Ubuntu 22.04, Ubuntu 24.04, Windows Server 2022, Windows Server
   2025, and Windows 11 with Python 3.12.
4. Certify the 100,000-memory latency targets on the 8-vCPU/16-GB/100-GB-SSD
   baseline.
5. Complete forced Ollama/NATS outage tests, a 24-hour soak, an encrypted off-host
   backup, and a clean-server full restore.
6. Create a 2.1 tag so the release workflow can reproduce, SBOM, sign, verify,
   and publish the wheel. Do not distribute an unsigned ad-hoc wheel as a
   production release.

## Path above 9

The phased execution plan, evidence layout, and definition of done are maintained
in [Production certification plan](production-certification-plan.md).

Treat every item above as a required release gate. Store CI reports, coverage XML,
SBOM, Sigstore bundles, benchmark output, soak logs, database integrity output,
and restore-drill results as release evidence. When all six gates pass with no
P0/P1 defects or high-severity dependency findings, the expected audited score is
9.3-9.5/10.

