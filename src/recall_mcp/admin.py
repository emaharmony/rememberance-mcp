"""Administrative CLI for Recall production operations."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from contextlib import contextmanager
import json
import os
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from recall_mcp.config import Settings
from recall_mcp.api.security import verify_token_file_permissions

from recall_mcp.store import MemoryStore
from recall_mcp.store.migrations import run_migrations

VERSION = "2.1.0"


def _sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(str(source))
    destination_conn = sqlite3.connect(str(destination))
    try:
        source_conn.backup(destination_conn)
    finally:
        destination_conn.close()
        source_conn.close()


def _integrity(path: Path) -> str:
    if not path.exists():
        return "missing"
    with sqlite3.connect(str(path)) as connection:
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_secret(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value + "\n", encoding="utf-8")
    if os.name != "nt":
        temporary.chmod(0o600)
    os.replace(temporary, path)


def _run_nats_cli(settings: Settings, args: argparse.Namespace, *command: str) -> None:
    executable = str(getattr(args, "nats_cli", "nats"))
    invocation = [executable, "--server", settings.NATS_URL]
    if settings.NATS_CREDS_FILE:
        invocation.extend(["--creds", str(settings.NATS_CREDS_FILE)])
    invocation.extend(command)
    subprocess.run(invocation, check=True)


def _verify_manifest(directory: Path) -> None:
    manifest_path = directory / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit("backup manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in manifest.get("files", []):
        candidate = directory / item["name"]
        if not candidate.is_file() or _sha256(candidate) != item["sha256"]:
            raise SystemExit(f"backup checksum failed for {item['name']}")


def _extract_backup_archive(archive: tarfile.TarFile, extraction_root: Path) -> None:
    """Extract only bounded regular files and directories below the target."""

    root = extraction_root.resolve()
    members = archive.getmembers()
    if len(members) > 10_000:
        raise SystemExit("backup archive contains too many entries")
    if sum(max(0, member.size) for member in members) > 100 * 1024**3:
        raise SystemExit("backup archive is too large")

    for member in members:
        target = (extraction_root / member.name).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise SystemExit("backup archive contains an unsafe path") from exc

        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not member.isfile():
            raise SystemExit("backup archive contains an unsafe entry")

        payload = archive.extractfile(member)
        if payload is None:
            raise SystemExit("backup archive contains an unreadable file")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("xb") as destination:
                shutil.copyfileobj(payload, destination, length=1024 * 1024)
        finally:
            payload.close()


@contextmanager
def _materialize_backup(source: Path, args: argparse.Namespace):
    if source.suffix.lower() != ".age":
        yield source
        return

    identity = getattr(args, "age_identity", None)
    if identity is None:
        raise SystemExit("--age-identity is required for encrypted backups")
    age_executable = str(getattr(args, "age_exe", "age"))
    with tempfile.TemporaryDirectory(prefix="recall-restore-") as temporary:
        temporary_path = Path(temporary)
        archive_path = temporary_path / "backup.tar.gz"
        subprocess.run(
            [
                age_executable,
                "--decrypt",
                "--identity",
                str(identity),
                "--output",
                str(archive_path),
                str(source),
            ],
            check=True,
        )
        extraction_root = temporary_path / "extracted"
        extraction_root.mkdir()
        with tarfile.open(archive_path, "r:gz") as archive:
            _extract_backup_archive(archive, extraction_root)

        children = list(extraction_root.iterdir())
        materialized = (
            children[0]
            if len(children) == 1 and children[0].is_dir()
            else extraction_root
        )
        yield materialized


def _encrypt_backup(destination: Path, recipient: str, age_executable: str) -> Path:
    archive = Path(
        shutil.make_archive(
            str(destination),
            "gztar",
            root_dir=destination.parent,
            base_dir=destination.name,
        )
    )
    encrypted = destination.parent / f"{destination.name}.tar.gz.age"
    try:
        subprocess.run(
            [
                age_executable,
                "--recipient",
                recipient,
                "--output",
                str(encrypted),
                str(archive),
            ],
            check=True,
        )
    finally:
        archive.unlink(missing_ok=True)
    return encrypted


def command_init(settings: Settings, args: argparse.Namespace) -> int:
    for name in ("models", "brain", "backups", "logs", "secrets"):
        (settings.BASE_DIR / name).mkdir(parents=True, exist_ok=True)
    token_file = args.token_file or settings.API_TOKEN_FILE
    if token_file is None:
        token_file = settings.BASE_DIR / "secrets" / "api-token"
    if not token_file.exists():
        _write_secret(token_file, secrets.token_urlsafe(48))
    from recall_mcp.pipeline import MemoryPipeline

    with MemoryPipeline(settings, start_outbox_worker=False):
        pass
    print(json.dumps({"home": str(settings.BASE_DIR), "token_file": str(token_file)}))
    return 0


def command_doctor(settings: Settings, args: argparse.Namespace) -> int:
    from recall_mcp.cag import CAGDeliveryService
    from recall_mcp.context import ContextPackService
    from recall_mcp.continuity import ContinuityStore, SessionService, TaskService
    from recall_mcp.feedback import RetrievalFeedbackService
    from recall_mcp.handoff import HandoffService
    from recall_mcp.skills import SkillService

    store = MemoryStore(settings.DB_PATH)
    integrity = store.integrity_report(quick=True)
    operational = store.operational_stats()
    feedback = RetrievalFeedbackService(settings.DB_PATH, settings)
    continuity = ContinuityStore(settings.DB_PATH)
    tasks = TaskService(continuity)
    sessions = SessionService(continuity, tasks)
    skills = SkillService(settings.DB_PATH, settings)
    context_service = ContextPackService(
        settings.DB_PATH,
        settings,
        tasks,
        sessions,
        feedback,
        search=lambda **_kwargs: ([], "doctor-probe"),
        skill_service=skills,
    )
    checks: dict[str, object] = {
        "version": VERSION,
        "home": str(settings.BASE_DIR),
        "database": str(settings.DB_PATH),
        "database_integrity": integrity,
        "outbox": operational["outbox"],
        "outbox_active_leases": operational["outbox_active_leases"],
        "outbox_oldest_due_seconds": operational["outbox_oldest_due_seconds"],
        "outbox_worker": {
            "observed": False,
            "running": False,
            "thread_alive": False,
            "inflight": 0,
            "last_error": None,
        },
        "retrieval_feedback": feedback.telemetry_stats(),
        "context_service": context_service.health(),
        "skill_service": skills.health(),
        "handoff_service": HandoffService(
            settings.DB_PATH,
            settings,
            tasks,
            sessions,
            context_service,
            skills,
        ).health(),
    }
    handoffs = HandoffService(
        settings.DB_PATH,
        settings,
        tasks,
        sessions,
        context_service,
        skills,
    )
    checks["cag_service"] = CAGDeliveryService(
        settings.DB_PATH,
        settings,
        tasks,
        sessions,
        context_service,
        skills,
        handoffs,
    ).health()
    token_file = args.token_file or settings.API_TOKEN_FILE
    checks["token_file"] = str(token_file) if token_file else None
    checks["token_present"] = bool(
        settings.API_TOKEN or (token_file and token_file.exists())
    )
    checks["token_permissions_ok"] = (
        True if token_file is None else verify_token_file_permissions(token_file)
    )

    try:
        with urllib.request.urlopen(
            f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/tags", timeout=3
        ) as response:
            checks["ollama"] = response.status == 200
    except (OSError, urllib.error.URLError):
        checks["ollama"] = False

    network_safe = (
        settings.HOST in {"127.0.0.1", "::1", "localhost"} or checks["token_present"]
    )
    checks["ready"] = bool(
        integrity["ok"] and network_safe and checks["token_permissions_ok"]
    )
    print(json.dumps(checks, indent=2))
    return 0 if checks["ready"] else 1


def command_migrate(settings: Settings, _args: argparse.Namespace) -> int:
    result = run_migrations(settings.DB_PATH)
    print(json.dumps(result.as_dict(), indent=2))
    return 0


def command_models_pull(settings: Settings, _args: argparse.Namespace) -> int:
    models = list(dict.fromkeys([settings.EXTRACT_MODEL, settings.EMBED_MODEL]))
    for model in models:
        payload = json.dumps({"name": model, "stream": False}).encode("utf-8")
        request = urllib.request.Request(
            f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/pull",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(
            request, timeout=max(300, settings.OLLAMA_TIMEOUT_SECONDS)
        ):
            pass
        print(f"pulled {model}")
    return 0


async def _bootstrap_nats(settings: Settings) -> None:
    import nats
    from nats.js.api import AckPolicy, ConsumerConfig, StorageType
    from nats.js.errors import NotFoundError

    options: dict[str, Any] = {"servers": [settings.NATS_URL]}
    if settings.NATS_CREDS_FILE:
        options["user_credentials"] = str(settings.NATS_CREDS_FILE)
    client = await nats.connect(**options)
    try:
        jetstream = client.jetstream()
        required_subjects = {
            settings.NATS_SUBJECT,
            settings.NATS_DLQ_SUBJECT,
        }
        try:
            stream_info = await jetstream.stream_info(settings.NATS_STREAM)
        except NotFoundError:
            await jetstream.add_stream(
                name=settings.NATS_STREAM,
                subjects=sorted(required_subjects),
                storage=StorageType.FILE,
            )
        else:
            configured = set(stream_info.config.subjects or [])
            if not required_subjects.issubset(configured):
                stream_info.config.subjects = sorted(configured | required_subjects)
                await jetstream.update_stream(config=stream_info.config)

        try:
            await jetstream.consumer_info(settings.NATS_STREAM, settings.NATS_CONSUMER)
        except NotFoundError:
            config = ConsumerConfig(
                durable_name=settings.NATS_CONSUMER,
                filter_subject=settings.NATS_SUBJECT,
                ack_policy=AckPolicy.EXPLICIT,
                ack_wait=60,
                max_ack_pending=64,
                max_deliver=10,
                backoff=[1, 5, 15, 30, 60, 120, 300, 300, 300, 300],
            )
            await jetstream.add_consumer(settings.NATS_STREAM, config)
    finally:
        await client.drain()


def command_nats_bootstrap(settings: Settings, _args: argparse.Namespace) -> int:
    asyncio.run(_bootstrap_nats(settings))
    print(f"bootstrapped {settings.NATS_STREAM}/{settings.NATS_CONSUMER}")
    return 0


def command_token_rotate(settings: Settings, args: argparse.Namespace) -> int:
    current = args.token_file or settings.API_TOKEN_FILE
    if current is None:
        raise SystemExit("configure --token-file or RECALL_API_TOKEN_FILE")
    previous = settings.PREVIOUS_API_TOKEN_FILE or current.with_name(
        current.name + ".previous"
    )
    if current.exists():
        previous.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(current, previous)
        if os.name != "nt":
            previous.chmod(0o600)
    _write_secret(current, secrets.token_urlsafe(48))
    print(json.dumps({"current": str(current), "previous": str(previous)}))
    return 0


def command_backup(settings: Settings, args: argparse.Namespace) -> int:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    requested = getattr(args, "destination", None)
    destination = requested or settings.BASE_DIR / "backups" / stamp
    destination.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, str]] = []
    for source in (settings.DB_PATH, settings.BASE_DIR / "metrics.db"):
        if not source.exists():
            continue
        target = destination / source.name
        _sqlite_backup(source, target)
        files.append({"name": target.name, "sha256": _sha256(target)})

    nats_included = bool(getattr(args, "include_nats", False))
    if nats_included:
        _run_nats_cli(
            settings,
            args,
            "stream",
            "backup",
            settings.NATS_STREAM,
            str(destination / "nats"),
        )
        for snapshot_file in sorted((destination / "nats").rglob("*")):
            if snapshot_file.is_file():
                files.append(
                    {
                        "name": snapshot_file.relative_to(destination).as_posix(),
                        "sha256": _sha256(snapshot_file),
                    }
                )

    manifest = {
        "version": VERSION,
        "created_at": time.time(),
        "source_home": str(settings.BASE_DIR),
        "nats_included": nats_included,
        "files": files,
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    encrypted = None
    recipient = getattr(args, "age_recipient", None)
    if recipient:
        encrypted = _encrypt_backup(
            destination,
            recipient,
            str(getattr(args, "age_exe", "age")),
        )
    print(
        json.dumps(
            {
                "backup": str(destination),
                "encrypted": str(encrypted) if encrypted else None,
            }
        )
    )
    return 0


def command_restore(settings: Settings, args: argparse.Namespace) -> int:
    with _materialize_backup(args.source, args) as materialized:
        if materialized.is_dir():
            _verify_manifest(materialized)
            source = materialized / "memory.db"
            nats_backup = materialized / "nats"
        else:
            source = materialized
            nats_backup = None

        if _integrity(source) != "ok":
            raise SystemExit("backup database failed integrity_check")
        if settings.DB_PATH.exists() and not getattr(args, "force", False):
            raise SystemExit("target exists; pass --force after taking a backup")
        _sqlite_backup(source, settings.DB_PATH)

        if (
            getattr(args, "include_nats", False)
            and nats_backup is not None
            and nats_backup.exists()
        ):
            _run_nats_cli(settings, args, "stream", "restore", str(nats_backup))

    report = MemoryStore(settings.DB_PATH).integrity_report()
    print(json.dumps({"restored": str(settings.DB_PATH), **report}))
    return 0 if report["ok"] else 1


def command_integrity(settings: Settings, _args: argparse.Namespace) -> int:
    result = MemoryStore(settings.DB_PATH).integrity_report()
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def command_reembed(settings: Settings, args: argparse.Namespace) -> int:
    from recall_mcp.pipeline import MemoryPipeline

    with MemoryPipeline(settings, start_outbox_worker=False) as pipeline:
        result = pipeline.dream(phases=["embed_stale"], dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] in {"ok", "partial"} else 1


def command_outbox(settings: Settings, args: argparse.Namespace) -> int:
    """Inspect or retry durable capture-processing jobs."""
    store = MemoryStore(settings.DB_PATH)
    if args.action == "status":
        operational = store.operational_stats()
        print(
            json.dumps(
                {
                    "database": str(settings.DB_PATH),
                    "counts": operational["outbox"],
                    "active_leases": operational["outbox_active_leases"],
                    "oldest_due_seconds": operational["outbox_oldest_due_seconds"],
                },
                indent=2,
            )
        )
        return 0
    if not args.job_id:
        print(json.dumps({"error": "outbox retry requires a job ID"}))
        return 2
    retried = store.retry_dead_outbox_job(args.job_id)
    print(json.dumps({"job_id": args.job_id, "requeued": retried}))
    return 0 if retried else 1


def command_install_hooks(_settings: Settings, args: argparse.Namespace) -> int:
    """Wire up an agent's MCP registration and hooks with no hand-editing."""
    from recall_mcp.install_hooks import run_install_hooks

    result = run_install_hooks(
        agents=args.agent,
        dry_run=args.dry_run,
        scope=args.scope,
        force=args.force,
        skip_mcp=args.skip_mcp,
        skip_hooks=args.skip_hooks,
        config_path=args.config_path,
        settings_path=args.settings_path,
    )
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def command_utility(settings: Settings, args: argparse.Namespace) -> int:
    """Inspect shadow utility and perform audited, reversible lifecycle actions."""
    from recall_mcp.feedback import RetrievalFeedbackService

    service = RetrievalFeedbackService(settings.DB_PATH, settings)
    if args.action == "shadow-report":
        result = service.shadow_report(limit=args.limit)
    else:
        if not args.memory_id:
            print(json.dumps({"error": f"utility {args.action} requires a memory ID"}))
            return 2
        if args.action == "inspect":
            result = service.explain_utility(args.memory_id)
        elif args.action == "cold":
            result = service.demote_to_cold(
                args.memory_id, reason=args.reason or "manual"
            )
        elif args.action == "restore":
            result = service.restore_from_cold(args.memory_id)
        elif args.action == "pin":
            result = service.pin(args.memory_id)
        elif args.action == "unpin":
            result = service.unpin(args.memory_id)
        else:  # pragma: no cover - argparse enforces the choices
            raise ValueError("unsupported utility action")
    print(json.dumps(result, indent=2))
    return 0


def command_context(settings: Settings, args: argparse.Namespace) -> int:
    """Inspect persisted context-pack metadata without starting a worker."""
    from recall_mcp.context import ContextPackService
    from recall_mcp.continuity import (
        ContinuityError,
        ContinuityStore,
        SessionService,
        TaskService,
    )
    from recall_mcp.feedback import RetrievalFeedbackService

    continuity = ContinuityStore(settings.DB_PATH)
    tasks = TaskService(continuity)
    service = ContextPackService(
        settings.DB_PATH,
        settings,
        tasks,
        SessionService(continuity, tasks),
        RetrievalFeedbackService(settings.DB_PATH, settings),
    )
    try:
        result = service.inspect(args.context_pack_id, explain=args.action == "explain")
    except ContinuityError as exc:
        print(json.dumps({"error": str(exc), "code": exc.code}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


def _cag_admin_scope(args: argparse.Namespace) -> dict[str, str | None]:
    return {
        "user_id": args.user_id,
        "workspace_id": args.workspace_id,
        "project_id": args.project_id,
        "repository_id": args.repository_id,
        "task_id": args.task_id,
        "session_id": args.session_id,
        "agent_id": args.agent_id,
    }


def command_cache(settings: Settings, args: argparse.Namespace) -> int:
    """Inspect or invalidate disposable CAG cache metadata."""
    from recall_mcp.continuity import ContinuityError
    from recall_mcp.pipeline import MemoryPipeline

    try:
        with MemoryPipeline(settings, start_outbox_worker=False) as pipeline:
            service = pipeline.cag_service
            if args.action == "status":
                result: object = service.stats()
            elif args.action == "list":
                result = {
                    "entries": service.inspect_cache(
                        _cag_admin_scope(args), status=args.status
                    )
                }
            elif args.action == "inspect":
                if not args.cache_entry_id:
                    raise ContinuityError("cache inspect requires a cache entry ID")
                result = service.inspect_cache_entry(
                    args.cache_entry_id, _cag_admin_scope(args)
                )
            elif args.action == "invalidate":
                result = service.invalidate_scope(
                    user_id=args.user_id,
                    workspace_id=args.workspace_id,
                    project_id=args.project_id,
                    repository_id=args.repository_id,
                    task_id=args.task_id,
                    session_id=args.session_id,
                    agent_id=args.agent_id,
                    cache_entry_id=args.cache_entry_id,
                    reason=args.reason or "manual_invalidation",
                    actor_id=args.actor_id,
                )
            elif args.action == "prune-expired":
                result = service.prune_expired()
            else:  # pragma: no cover - argparse enforces choices
                raise ContinuityError("unsupported cache action")
    except ContinuityError as exc:
        print(json.dumps({"error": str(exc), "code": exc.code}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


def command_delivery(settings: Settings, args: argparse.Namespace) -> int:
    """Inspect persisted CAG deliveries without starting a worker."""
    from recall_mcp.continuity import ContinuityError
    from recall_mcp.pipeline import MemoryPipeline

    try:
        with MemoryPipeline(settings, start_outbox_worker=False) as pipeline:
            if args.action == "stats":
                result: object = pipeline.cag_service.stats()
            elif not args.delivery_id:
                raise ContinuityError("delivery ID is required for this action")
            elif args.action == "inspect":
                result = pipeline.cag_service.get_delivery(
                    args.delivery_id, _cag_admin_scope(args)
                )
            elif args.action == "explain":
                result = pipeline.cag_service.explain(
                    args.delivery_id, _cag_admin_scope(args)
                )
            else:  # pragma: no cover - argparse enforces choices
                raise ContinuityError("unsupported delivery action")
    except ContinuityError as exc:
        print(json.dumps({"error": str(exc), "code": exc.code}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


def command_skill(settings: Settings, args: argparse.Namespace) -> int:
    """Review immutable skill versions without starting the capture worker."""
    from recall_mcp.continuity import ContinuityError
    from recall_mcp.skills import SkillProposal, SkillScope, SkillService

    service = SkillService(settings.DB_PATH, settings)
    scope = SkillScope(
        user_id=args.user_id or "",
        workspace_id=args.workspace_id or "",
        project_id=args.project_id or "",
        repository_id=args.repository_id,
    )
    try:
        if args.action in {"list", "stale"}:
            result: object = {
                "skills": service.list_skills(
                    scope=scope,
                    status="stale" if args.action == "stale" else args.status,
                )
            }
        elif args.action == "propose":
            if not all((args.slug, args.title, args.purpose, args.agent_id)):
                raise ContinuityError(
                    "propose requires --slug, --title, --purpose, and --agent-id"
                )
            sources: list[dict[str, object]] = [
                {"source_type": "memory", "source_id": memory_id}
                for memory_id in (args.memory_id or [])
            ]
            if args.source_ref and args.source_content:
                sources.append(
                    {
                        "source_type": "repository",
                        "source_id": args.repository_id,
                        "source_ref": args.source_ref,
                        "content": args.source_content,
                    }
                )
            result = service.propose(
                SkillProposal(
                    scope=scope,
                    slug=args.slug,
                    title=args.title,
                    purpose=args.purpose,
                    created_by_agent_id=args.agent_id,
                    sources=tuple(sources),
                    summary=args.summary or "",
                )
            )
        elif not args.skill_id:
            raise ContinuityError("skill_id is required for this action")
        elif args.action == "inspect":
            result = service.get(args.skill_id, scope=scope, version=args.version)
        elif args.action == "versions":
            result = {
                "skill_id": args.skill_id,
                "versions": service.versions(args.skill_id, scope=scope),
            }
        elif args.action == "evidence":
            skill = service.get(args.skill_id, scope=scope, version=args.version)
            result = {
                "skill_id": args.skill_id,
                "version": skill["version"],
                "evidence": service.evidence(
                    args.skill_id, int(skill["version"]), scope=scope
                ),
            }
        elif args.action == "approve":
            if args.version is None or not args.reviewer_id:
                raise ContinuityError("approve requires --version and --reviewer-id")
            result = service.approve(
                args.skill_id,
                args.version,
                scope=scope,
                reviewer_id=args.reviewer_id,
                reason=args.reason or "",
            )
        elif args.action == "reject":
            if args.version is None or not args.reviewer_id or not args.reason:
                raise ContinuityError(
                    "reject requires --version, --reviewer-id, and --reason"
                )
            result = service.reject(
                args.skill_id,
                args.version,
                scope=scope,
                reviewer_id=args.reviewer_id,
                reason=args.reason,
            )
        elif args.action == "refresh":
            if not args.agent_id:
                raise ContinuityError("refresh requires --agent-id")
            result = service.refresh(
                args.skill_id,
                scope=scope,
                created_by_agent_id=args.agent_id,
            )
        else:
            raise ContinuityError("unsupported skill action")
    except ContinuityError as exc:
        print(json.dumps({"error": str(exc), "code": exc.code}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


def command_handoff(settings: Settings, args: argparse.Namespace) -> int:
    """Inspect and recover handoffs without starting the capture worker."""
    from recall_mcp.context import ContextPackService
    from recall_mcp.continuity import (
        ContinuityError,
        ContinuityStore,
        SessionService,
        TaskService,
    )
    from recall_mcp.feedback import RetrievalFeedbackService
    from recall_mcp.handoff import HandoffScope, HandoffService
    from recall_mcp.skills import SkillService

    continuity = ContinuityStore(settings.DB_PATH)
    tasks = TaskService(continuity)
    sessions = SessionService(continuity, tasks)
    skills = SkillService(settings.DB_PATH, settings)
    context = ContextPackService(
        settings.DB_PATH,
        settings,
        tasks,
        sessions,
        RetrievalFeedbackService(settings.DB_PATH, settings),
        skill_service=skills,
    )
    service = HandoffService(
        settings.DB_PATH, settings, tasks, sessions, context, skills
    )
    scope = HandoffScope(
        user_id=args.user_id,
        workspace_id=args.workspace_id,
        project_id=args.project_id,
        repository_id=args.repository_id,
        task_id=args.task_id,
        session_id=args.session_id,
    )
    try:
        if args.action == "list":
            result: object = {
                "handoffs": service.list_handoffs(
                    scope=scope, agent_id=args.agent_id, status=args.status
                )
            }
        elif not args.handoff_id:
            raise ContinuityError("handoff_id is required for this action")
        elif args.action == "inspect":
            result = service.get(
                args.handoff_id,
                scope=scope,
                version=args.version,
                agent_id=args.agent_id,
            )
        elif args.action == "versions":
            result = {
                "handoff_id": args.handoff_id,
                "versions": service.versions(
                    args.handoff_id, scope=scope, agent_id=args.agent_id
                ),
            }
        elif args.action == "explain":
            result = service.explain(
                args.handoff_id,
                scope=scope,
                version=args.version,
                agent_id=args.agent_id,
            )
        elif args.action == "cancel":
            if not args.agent_id or not args.reason:
                raise ContinuityError("cancel requires --agent-id and --reason")
            result = service.cancel(
                args.handoff_id,
                scope=scope,
                agent_id=args.agent_id,
                reason=args.reason,
                idempotency_key=args.idempotency_key,
            )
        elif args.action == "expire":
            if not args.agent_id:
                raise ContinuityError("expire requires --agent-id")
            result = service.expire(
                args.handoff_id,
                scope=scope,
                agent_id=args.agent_id,
                reason=args.reason or "manual expiration",
                idempotency_key=args.idempotency_key,
            )
        elif args.action == "retry":
            if not args.agent_id:
                raise ContinuityError("retry requires --agent-id")
            result = service.progress(
                args.handoff_id,
                scope=scope,
                agent_id=args.agent_id,
                progress={"reason": args.reason or "manual retry"},
                idempotency_key=args.idempotency_key,
            )
        else:  # pragma: no cover - argparse enforces choices
            raise ContinuityError("unsupported handoff action")
    except ContinuityError as exc:
        print(json.dumps({"error": str(exc), "code": exc.code}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="recall-admin")
    parser.add_argument("--home", type=Path)
    parser.add_argument("--token-file", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init")
    subparsers.add_parser("doctor")
    subparsers.add_parser("migrate")
    outbox = subparsers.add_parser("outbox")
    outbox.add_argument("action", choices=["status", "retry"])
    outbox.add_argument("job_id", nargs="?")
    install_hooks = subparsers.add_parser(
        "install-hooks",
        description=(
            "Register the Recall MCP server and SessionStart/Stop hooks for an "
            "agent (Claude Code, Codex) directly from an installed recall-mcp "
            "-- no source checkout, no hand-editing of JSON/TOML."
        ),
    )
    install_hooks.add_argument(
        "--agent",
        action="append",
        required=True,
        choices=["claude-code", "codex", "all"],
        help="Agent to wire up. May be given more than once; 'all' wires up every known agent.",
    )
    install_hooks.add_argument(
        "--dry-run", action="store_true", help="Print the plan; write nothing."
    )
    install_hooks.add_argument(
        "--scope",
        choices=["user", "local", "project"],
        default="user",
        help="MCP registration scope (claude-code only; ignored by codex, which has no scope concept).",
    )
    install_hooks.add_argument(
        "--force",
        action="store_true",
        help="Attempt a best-effort rename for an unrecognized 'remembrance' command.",
    )
    install_hooks.add_argument(
        "--skip-mcp", action="store_true", help="Skip MCP server registration."
    )
    install_hooks.add_argument(
        "--skip-hooks", action="store_true", help="Skip SessionStart/Stop hook wiring."
    )
    install_hooks.add_argument(
        "--config-path",
        type=Path,
        help="Override the MCP registration file (claude-code only; e.g. a scratch copy of ~/.claude.json).",
    )
    install_hooks.add_argument(
        "--settings-path",
        type=Path,
        help="Override the hooks file (~/.claude/settings.json or ~/.codex/hooks.json).",
    )
    utility = subparsers.add_parser("utility")
    utility.add_argument(
        "action",
        choices=["shadow-report", "inspect", "cold", "restore", "pin", "unpin"],
    )
    utility.add_argument("memory_id", nargs="?")
    utility.add_argument("--reason")
    utility.add_argument("--limit", type=int, default=100)
    context = subparsers.add_parser("context")
    context.add_argument("action", choices=["inspect", "explain"])
    context.add_argument("context_pack_id")
    cache = subparsers.add_parser("cache")
    cache.add_argument(
        "action", choices=["status", "list", "inspect", "invalidate", "prune-expired"]
    )
    cache.add_argument("cache_entry_id", nargs="?")
    cache.add_argument("--user-id")
    cache.add_argument("--workspace-id")
    cache.add_argument("--project-id")
    cache.add_argument("--repository-id")
    cache.add_argument("--task-id")
    cache.add_argument("--session-id")
    cache.add_argument("--agent-id")
    cache.add_argument("--actor-id")
    cache.add_argument("--status")
    cache.add_argument("--reason")
    delivery = subparsers.add_parser("delivery")
    delivery.add_argument("action", choices=["inspect", "explain", "stats"])
    delivery.add_argument("delivery_id", nargs="?")
    delivery.add_argument("--user-id")
    delivery.add_argument("--workspace-id")
    delivery.add_argument("--project-id")
    delivery.add_argument("--repository-id")
    delivery.add_argument("--task-id")
    delivery.add_argument("--session-id")
    delivery.add_argument("--agent-id")
    skill = subparsers.add_parser("skill")
    skill.add_argument(
        "action",
        choices=[
            "list",
            "inspect",
            "versions",
            "propose",
            "approve",
            "reject",
            "refresh",
            "evidence",
            "stale",
        ],
    )
    skill.add_argument("skill_id", nargs="?")
    skill.add_argument("--version", type=int)
    skill.add_argument("--user-id", required=True)
    skill.add_argument("--workspace-id", required=True)
    skill.add_argument("--project-id", required=True)
    skill.add_argument("--repository-id")
    skill.add_argument("--status")
    skill.add_argument("--slug")
    skill.add_argument("--title")
    skill.add_argument("--purpose")
    skill.add_argument("--summary")
    skill.add_argument("--agent-id")
    skill.add_argument("--reviewer-id")
    skill.add_argument("--reason")
    skill.add_argument("--memory-id", action="append")
    skill.add_argument("--source-ref")
    skill.add_argument("--source-content")
    handoff = subparsers.add_parser("handoff")
    handoff.add_argument(
        "action",
        choices=[
            "list",
            "inspect",
            "versions",
            "explain",
            "cancel",
            "expire",
            "retry",
        ],
    )
    handoff.add_argument("handoff_id", nargs="?")
    handoff.add_argument("--version", type=int)
    handoff.add_argument("--user-id", required=True)
    handoff.add_argument("--workspace-id", required=True)
    handoff.add_argument("--project-id", required=True)
    handoff.add_argument("--repository-id", required=True)
    handoff.add_argument("--task-id", required=True)
    handoff.add_argument("--session-id", required=True)
    handoff.add_argument("--agent-id")
    handoff.add_argument("--status")
    handoff.add_argument("--reason")
    handoff.add_argument("--idempotency-key")
    models = subparsers.add_parser("models")
    models.add_argument("action", choices=["pull"])
    nats_parser = subparsers.add_parser("nats")
    nats_parser.add_argument("action", choices=["bootstrap"])
    token = subparsers.add_parser("token")
    token.add_argument("action", choices=["rotate"])
    backup = subparsers.add_parser("backup")
    backup.add_argument("destination", type=Path, nargs="?")
    backup.add_argument("--include-nats", action="store_true")
    backup.add_argument("--nats-cli", default="nats")
    backup.add_argument("--age-recipient")
    backup.add_argument("--age-exe", default="age")
    restore = subparsers.add_parser("restore")
    restore.add_argument("source", type=Path)
    restore.add_argument("--force", action="store_true")
    restore.add_argument("--include-nats", action="store_true")
    restore.add_argument("--nats-cli", default="nats")
    restore.add_argument("--age-identity", type=Path)
    restore.add_argument("--age-exe", default="age")
    subparsers.add_parser("integrity-check")
    reembed = subparsers.add_parser("reembed")
    reembed.add_argument("--dry-run", action="store_true")
    subparsers.add_parser("version")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.home:
        os.environ["RECALL_HOME"] = str(args.home)
    if args.token_file:
        os.environ["RECALL_API_TOKEN_FILE"] = str(args.token_file)
    settings = Settings()

    commands = {
        "init": command_init,
        "doctor": command_doctor,
        "migrate": command_migrate,
        "backup": command_backup,
        "restore": command_restore,
        "integrity-check": command_integrity,
        "reembed": command_reembed,
        "install-hooks": command_install_hooks,
    }
    if args.command == "models":
        code = command_models_pull(settings, args)
    elif args.command == "nats":
        code = command_nats_bootstrap(settings, args)
    elif args.command == "token":
        code = command_token_rotate(settings, args)
    elif args.command == "outbox":
        code = command_outbox(settings, args)
    elif args.command == "utility":
        code = command_utility(settings, args)
    elif args.command == "context":
        code = command_context(settings, args)
    elif args.command == "cache":
        code = command_cache(settings, args)
    elif args.command == "delivery":
        code = command_delivery(settings, args)
    elif args.command == "skill":
        code = command_skill(settings, args)
    elif args.command == "handoff":
        code = command_handoff(settings, args)
    elif args.command == "version":
        print(VERSION)
        code = 0
    else:
        code = commands[args.command](settings, args)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
