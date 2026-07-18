# Recall 2.1 path to production certification

Saved: 2026-07-17  
Current audited readiness: 8.7/10  
Target: at least 9.3/10 with every Recall 2.1 release gate proven.

This is the execution plan for the active goal. The feature implementation is a
release candidate; the remaining work is assurance, real-host certification,
performance qualification, and release evidence. The goal stays active until
all completion criteria below pass.

## Groundwork already complete

- Recall rename with Remembrance compatibility through 2.1.
- Typed validated settings, loopback defaults, bearer authentication, token
  rotation overlap, request/rate/concurrency limits, safe errors, CORS controls,
  liveness, readiness, metrics, and JSON logging.
- Raw-first bounded asynchronous capture, migrations, foreign keys, cascades,
  idempotent event storage, integrity/orphan checks, scoped retrieval, facts,
  provenance, supersession, contradiction resolution, Ollama embeddings, and
  exact sqlite-vec retrieval.
- Durable JetStream design with explicit acknowledgement, retry backoff,
  redelivery, DLQ, bounded pending delivery, and database idempotency.
- Administrative CLI, safe archive extraction, encrypted backup/restore,
  JetStream snapshots, Ubuntu and Windows deployment assets, SBOM and signed
  release workflows, architecture decisions, and operational runbooks.
- Local baseline: 187 tests pass, 2 skip, 62% total line coverage, Ruff and Mypy
  pass, Bandit has zero high-severity findings, pip-audit has no known installed
  vulnerabilities, and package/Compose/PowerShell/YAML checks pass.

## Workstream 1: assurance coverage

Objective: reach at least 90% line coverage, 85% branch coverage, and 95% for
security, storage, capture, and delivery without excluding production code.

### 1.1 REST and security matrix

Add behavior tests for:

- all authenticated and unauthenticated routes;
- malformed JSON, missing fields, wrong types, body and capture limits;
- read/write rate-window expiration and client isolation;
- local versus remote readiness access;
- exact CORS allowlists and preflight behavior;
- current, previous, expired, missing, unreadable, and unsafe token files;
- secure headers and exception redaction;
- concurrent REST reads during capture and maintenance.

Primary files: `api/rest.py`, `api/security.py`.  
Exit evidence: focused coverage report at or above 95% for both modules.

### 1.2 Capture, storage, and migration matrix

Add tests for:

- every gate outcome and extractor/graph/fact/embedding failure branch;
- pending completion, executor submission failure, queue saturation, and restart
  recovery of pending raw captures;
- legacy schema upgrades from each supported pre-2.1 shape;
- foreign-key enforcement, all cascades, orphan detection, WAL/busy handling,
  event reservation reclamation, expiry, consolidation, and scoped queries;
- FactStore dry runs, ties, provenance, supersession chains, and contradictions;
- stale vectors caused by content, model, dimensions, hash, and status changes.

Primary files: `pipeline.py`, `store/*.py`, `dream/cycle.py`,
`embeddings.py`.  
Exit evidence: the critical capture/storage group is at least 95%.

### 1.3 Delivery and administration matrix

Extend mocked and live tests for:

- stream creation and update, consumer configuration, reconnect callbacks,
  timeout/backlog refresh, graceful drain, and subscriber thread lifecycle;
- invalid UTF-8, non-object payloads, oversize events, duplicate IDs, capture
  failures, each delivery threshold, DLQ publish failure, and process restart;
- every `recall-admin` command, idempotent reruns, manifest tampering, archive
  bombs, missing tools, age failures, NATS snapshot failure, force semantics,
  migration, re-embedding, token rotation, doctor, and version.

Primary files: `nats_jetstream.py`, `admin.py`.  
Exit evidence: delivery paths are at least 95%; administration is at least 90%.

### 1.4 Coverage milestones

- Milestone A: 75% overall with critical modules above 85%.
- Milestone B: 85% overall with critical modules above 92%.
- Milestone C: 90% line, 85% branch, and 95% critical coverage.

Do not lower thresholds, omit production modules, or count import-only tests as
completion.

## Workstream 2: live dependency and failure integration

Objective: prove behavior against pinned Ollama, NATS 2.11.8, and sqlite-vec
0.1.9 rather than mocks alone.

- Run the live JetStream redelivery/idempotency test.
- Verify stream and consumer settings through the NATS API.
- Kill and restart Recall between receive and acknowledgement.
- Force NATS disconnects, reconnects, backlog growth, and DLQ exhaustion.
- Stop Ollama during capture/search/re-embedding and verify durable recovery.
- Exercise token rotation while requests are active.
- Corrupt disposable database and backup copies and verify fail-closed behavior.
- Upgrade a fixture database, roll back with its matching backup, and rerun the
  old version.
- Restore SQLite, configuration, secrets, and JetStream onto a clean stack.

Exit evidence: integration logs, readiness snapshots, event counts, integrity
reports, and a proof that no acknowledged event was lost.

## Workstream 3: supported-platform certification

Objective: certify clean install, upgrade, rollback, backup, restore, and removal
on every supported host.

| Platform | Runner strategy | Required profiles |
| --- | --- | --- |
| Ubuntu 22.04 | CI VM or clean cloud VM | CPU and GPU override |
| Ubuntu 24.04 | GitHub-hosted plus clean VM | CPU and GPU override |
| Windows Server 2022 | GitHub-hosted plus clean VM | Native services |
| Windows Server 2025 | Self-hosted or cloud VM | Native services |
| Windows 11 | Self-hosted physical/VM runner | Native services |

For each platform record:

1. machine image, CPU, RAM, disk, Python, Docker/WinSW/NATS/Ollama versions;
2. clean installation and service auto-start after reboot;
3. loopback-only listeners, firewall rules, Tailscale Serve, and bearer auth;
4. model pulls, migration, NATS bootstrap, capture, all search modes, metrics;
5. encrypted off-host backup and full clean-host restore;
6. in-place upgrade, forced failure, rollback, and complete removal.

Exit evidence: signed platform checklist with command output and restored memory
and event counts.

## Workstream 4: performance and reliability qualification

Objective: certify the documented 100,000-memory baseline on 8 vCPU, 16 GB RAM,
and 100 GB SSD; use 32 GB RAM for the CPU-only model profile when needed.

Build deterministic tools for:

- generating 100,000 scoped memories, entities, facts, and embeddings;
- measuring warm-up separately from steady state;
- recording p50, p95, p99, throughput, CPU, RAM, disk, WAL, and NATS backlog;
- checking result correctness, not only latency;
- running a 24-hour mixed capture/search/dream/backup workload;
- injecting Ollama, NATS, disk-pressure, network, and process outages.

Required results:

- liveness/readiness p95 below 200 ms;
- keyword search p95 below 250 ms;
- warm hybrid search p95 below 2 seconds;
- zero lost acknowledged NATS messages;
- no unbounded memory, queue, WAL, database, or backlog growth;
- successful integrity check, off-host backup, and clean restore after soak.

## Workstream 5: supply chain and signed release

Objective: produce a reproducible, auditable 2.1 release.

- Run lint, type checking, Bandit, pip-audit, coverage, and integration gates on
  the release commit.
- Confirm zero P0/P1 defects and zero high-severity dependency findings.
- Build twice with the same `SOURCE_DATE_EPOCH` and compare wheel hashes.
- Install the wheel into clean Python 3.12 environments on Ubuntu and Windows.
- Generate and inspect the CycloneDX SBOM.
- Tag the exact commit, create the keyless Sigstore bundle, verify repository,
  workflow, tag identity, and transparency evidence, then publish checksums.
- Verify Ubuntu deployment from the tag and Windows installation from the signed
  wheel rather than the working tree.
- Archive all gate evidence with the release.

## Evidence layout

Generated evidence must not be committed as source. Store it in the release
artifact system using this logical layout:

```text
recall-2.1-evidence/
  manifest.json
  coverage/
  security/
  dependencies/
  sbom/
  signatures/
  integration/
  platforms/
  performance/
  soak/
  backup-restore/
  integrity/
```

The manifest records the release commit, artifact hashes, tool versions, host
images, start/end timestamps, and pass/fail result for every gate.

## Definition of done

Recall is above 9/10 only when all of the following are true:

- all documented 2.1 functionality and compatibility tests pass;
- coverage is at least 90% line, 85% branch, and 95% on critical paths;
- every supported Ubuntu and Windows target passes clean install, upgrade,
  rollback, backup, restore, reboot, and removal;
- the 100,000-memory performance targets and 24-hour soak pass;
- forced dependency outages recover without lost acknowledged events;
- database integrity and a complete clean-server restore pass;
- there are no P0/P1 defects or high dependency findings;
- reproducible artifacts, SBOM, checksums, and verified Sigstore bundles exist;
- architecture, configuration, API, migration, operations, deployment, and
  release documentation match the shipped artifact.

## Next working session

Start with Workstream 1.3 because delivery coverage is the largest critical risk.
Extend `tests/test_nats_delivery.py` to cover stream provisioning, reconnects,
backlog refresh, lifecycle, and publish/ack failure ordering. Then cover admin
backup/restore failure branches before moving to the REST/security matrix.
Re-run the full branch-coverage report after each milestone and update
`docs/release-readiness.md` only from measured evidence.

