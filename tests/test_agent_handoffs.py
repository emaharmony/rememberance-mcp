from __future__ import annotations

import json
import sqlite3
from argparse import Namespace
from pathlib import Path

import pytest

from recall_mcp.continuity import ContinuityError
from recall_mcp.admin import command_handoff
from recall_mcp.handoff import (
    HandoffCompletion,
    HandoffRequest,
    HandoffScope,
)
from recall_mcp.pipeline import MemoryPipeline
from recall_mcp.runtime import Settings
from recall_mcp.skills import SkillProposal, SkillScope
from recall_mcp.store.migrations import MIGRATIONS, run_migrations


@pytest.fixture
def handoff_env(tmp_path, monkeypatch):
    settings = Settings(
        BASE_DIR=tmp_path,
        DB_PATH=tmp_path / "memory.db",
        EMBEDDINGS_ENABLED=False,
        CONTEXT_INLINE_EVIDENCE_MAX_TOKENS=20,
    )
    pipeline = MemoryPipeline(settings, start_outbox_worker=False)
    task = pipeline.task_service.create_task(
        user_id="user-1",
        workspace_id="workspace-1",
        project_id="project-1",
        repository_id="repo-1",
        title="Implement agent handoffs",
        objective="Implement architecture handoffs with immutable versions",
        created_by="claude-code",
        canonical_path="/work/recall",
        default_branch="feature/recall-phase-5-agent-handoffs",
    )
    session = pipeline.session_service.start_session(
        task_id=task["id"], agent_id="claude-code"
    )
    pipeline.session_service.join_session(session["id"], agent_id="codex")
    checkpoint = pipeline.session_service.create_checkpoint(
        session["id"],
        agent_id="claude-code",
        summary="Ready for Codex",
        completed=["Phase 4"],
        remaining=["Phase 5"],
        constraints=["Do not begin Phase 6", "Preserve immutable skill versions"],
        approved_decisions=["Use shared application services"],
        proposed_decisions=["Add future notification adapters"],
        blockers=[],
        open_questions=["When should caches be enabled?"],
        important_files=["src/recall_mcp/handoff.py"],
        known_failures=["Historical stop hook lacked diagnostics"],
    )
    memory_text = "Repository evidence for handoff architecture. " * 80
    memory_id = pipeline.store.store(
        memory_text,
        memory_text,
        "project",
        "persist",
        [],
        source="repository:src/recall_mcp/handoff.py",
        agent="claude-code",
        user_id=task["user_id"],
        workspace_id=task["workspace_id"],
        project_id=task["project_id"],
        repository_id=task["repository_id"],
        task_id=task["id"],
        session_id=session["id"],
    )
    other = pipeline.task_service.create_task(
        user_id="user-1",
        workspace_id="workspace-1",
        project_id="project-2",
        repository_id="repo-2",
        title="Distractor",
        objective="Do not leak this project",
        created_by="claude-code",
        canonical_path="/work/other",
    )
    other_session = pipeline.session_service.start_session(
        task_id=other["id"], agent_id="claude-code"
    )
    distractor_id = pipeline.store.store(
        "Cross-project secret",
        "Cross-project secret",
        "project",
        "persist",
        [],
        user_id=other["user_id"],
        workspace_id=other["workspace_id"],
        project_id=other["project_id"],
        repository_id=other["repository_id"],
        task_id=other["id"],
        session_id=other_session["id"],
    )

    def scoped_search(_query, **kwargs):
        results = []
        for candidate in (memory_id, distractor_id):
            memory = pipeline.store.get(candidate)
            assert memory is not None
            if any(
                memory[field] != kwargs.get(field)
                for field in (
                    "user_id",
                    "workspace_id",
                    "project_id",
                    "repository_id",
                    "task_id",
                    "session_id",
                )
            ):
                continue
            results.append(
                {**memory, "score": 0.9, "keyword_score": 0.8, "sources": ["fts5"]}
            )
        return results

    monkeypatch.setattr(pipeline.hybrid_search, "search", scoped_search)
    skill_scope = SkillScope("user-1", "workspace-1", "project-1", "repo-1")
    candidate = pipeline.skill_service.propose(
        SkillProposal(
            scope=skill_scope,
            slug="handoff-architecture",
            title="Handoff Architecture",
            purpose="Guide immutable architecture handoffs",
            created_by_agent_id="claude-code",
            sources=({"source_type": "memory", "source_id": memory_id},),
            summary="Use immutable versions and scope-first lookup.",
            instructions=("Preserve target authorization",),
            constraints=("Do not replay full transcripts",),
        )
    )
    approved = pipeline.skill_service.approve(
        candidate["id"],
        1,
        scope=skill_scope,
        reviewer_id="user-1",
        reason="Verified by repository evidence",
    )
    scope = HandoffScope(
        "user-1",
        "workspace-1",
        "project-1",
        "repo-1",
        task["id"],
        session["id"],
    )
    yield {
        "settings": settings,
        "pipeline": pipeline,
        "task": task,
        "session": session,
        "checkpoint": checkpoint,
        "scope": scope,
        "skill": approved,
        "memory_id": memory_id,
        "distractor_id": distractor_id,
    }
    pipeline.close()


def request(env, **overrides):
    values = {
        "scope": env["scope"],
        "source_agent_id": "claude-code",
        "target_agent_id": "codex",
        "requested_by": "claude-code",
        "expected_output": "Tested implementation and completion report",
        "known_checkpoint_version": 0,
        "capabilities": ("structured_json", "markdown", "skill_references"),
        "idempotency_key": "handoff-create-1",
    }
    values.update(overrides)
    return HandoffRequest(**values)


def create_handoff(env, **overrides):
    return env["pipeline"].handoff_service.create(request(env, **overrides))


def test_migration_10_is_incremental_idempotent_and_immutable(tmp_path):
    db_path = tmp_path / "memory.db"
    run_migrations(db_path, MIGRATIONS[:9])
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO agents VALUES ('agent', 'Agent', 'other', 'trusted', 'standard', 1, 1)"
        )
        conn.execute(
            "INSERT INTO workspaces VALUES ('workspace', 'user', 'Workspace', 1, 1)"
        )
        conn.execute(
            "INSERT INTO projects VALUES ('project', 'user', 'workspace', 'Project', 'active', 1, 1)"
        )
        conn.execute(
            "INSERT INTO repositories VALUES ('repo', 'user', 'workspace', 'project', 'Repo', '/repo', NULL, NULL, 'read_only', 1, 1)"
        )
        conn.execute(
            "INSERT INTO tasks VALUES ('task', 'user', 'workspace', 'project', 'repo', 'Task', 'Objective', 'active', 'agent', NULL, 1, 1, NULL)"
        )
        conn.execute(
            "INSERT INTO sessions VALUES ('session', 'task', 'agent', 'active', 1, 1, NULL, '', 1, 2, NULL)"
        )
        conn.execute(
            "INSERT INTO session_events VALUES ('event', 'session', 'task', 'agent', 1, 'checkpoint.created', '{}', NULL, 1)"
        )
        conn.execute(
            """
            INSERT INTO session_checkpoints VALUES (
                'checkpoint', 'session', 1, 'Objective', '', '[]', '[]',
                '["constraint"]', '[]', '[]', '[]', '[]', '[]', '[]', '{}',
                'agent', 'event', 1, 0, 1
            )
            """
        )
    upgraded = run_migrations(db_path)
    repeated = run_migrations(db_path)
    assert [item.version for item in upgraded.applied] == [10, 11]
    assert repeated.from_version == repeated.to_version == 11
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        triggers = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
        checkpoint = conn.execute(
            "SELECT constraints_json FROM session_checkpoints WHERE id = 'checkpoint'"
        ).fetchone()
        event_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'session_events'"
        ).fetchone()[0]
    assert {
        "handoffs",
        "handoff_versions",
        "handoff_events",
        "handoff_completions",
        "handoff_references",
    } <= tables
    assert {"handoff_versions_no_update", "handoff_events_no_update"} <= triggers
    assert checkpoint == ('["constraint"]',)
    assert "handoff.completed" in event_sql


def test_create_is_scope_safe_deterministic_and_idempotent(handoff_env):
    handoff = create_handoff(handoff_env)
    replay = create_handoff(handoff_env)
    assert handoff["version"] == 1
    assert handoff["status"] == "ready"
    assert replay["handoff_id"] == handoff["handoff_id"]
    assert replay["idempotent_replay"] is True
    assert handoff["task"]["objective"] == handoff_env["task"]["objective"]
    assert "Do not begin Phase 6" in handoff["constraints"]
    assert handoff["decisions"] == ["Use shared application services"]
    assert handoff["continuity"]["session_delta"]["to_version"] == 1
    assert all(item["status"] == "approved" for item in handoff["skills"])
    assert handoff["content_hash"].startswith("sha256:")
    assert handoff["source_fingerprint"].startswith("sha256:")
    assert "Cross-project secret" not in json.dumps(handoff)


def test_wrong_scope_and_wrong_agent_are_hidden(handoff_env):
    handoff = create_handoff(handoff_env)
    wrong_scope = HandoffScope(
        "user-1",
        "workspace-1",
        "project-2",
        "repo-2",
        handoff_env["task"]["id"],
        handoff_env["session"]["id"],
    )
    with pytest.raises(ContinuityError):
        handoff_env["pipeline"].handoff_service.get(
            handoff["handoff_id"], scope=wrong_scope, agent_id="claude-code"
        )
    with pytest.raises(ContinuityError, match="authorized"):
        handoff_env["pipeline"].handoff_service.get(
            handoff["handoff_id"],
            scope=handoff_env["scope"],
            agent_id="unknown-agent",
        )


def test_exclusive_claim_progress_and_invalid_transition(handoff_env):
    service = handoff_env["pipeline"].handoff_service
    handoff = create_handoff(handoff_env)
    claimed = service.claim(
        handoff["handoff_id"],
        scope=handoff_env["scope"],
        agent_id="codex",
        idempotency_key="claim-1",
    )
    replay = service.claim(
        handoff["handoff_id"],
        scope=handoff_env["scope"],
        agent_id="codex",
        idempotency_key="claim-1",
    )
    assert claimed["status"] == "claimed"
    assert replay["idempotent_replay"] is True
    with pytest.raises(ContinuityError):
        service.claim(
            handoff["handoff_id"],
            scope=handoff_env["scope"],
            agent_id="claude-code",
        )
    progress = service.progress(
        handoff["handoff_id"],
        scope=handoff_env["scope"],
        agent_id="codex",
        progress={"completed": ["service"]},
        idempotency_key="progress-1",
    )
    assert progress["status"] == "in_progress"


def test_block_and_resume_are_audited(handoff_env):
    service = handoff_env["pipeline"].handoff_service
    handoff = create_handoff(handoff_env)
    service.claim(handoff["handoff_id"], scope=handoff_env["scope"], agent_id="codex")
    blocked = service.block(
        handoff["handoff_id"],
        scope=handoff_env["scope"],
        agent_id="codex",
        blocker={"reason": "missing fixture"},
        idempotency_key="block-1",
    )
    resumed = service.progress(
        handoff["handoff_id"],
        scope=handoff_env["scope"],
        agent_id="codex",
        progress={"reason": "fixture restored"},
    )
    assert blocked["status"] == "blocked"
    assert resumed["status"] == "in_progress"


def test_expiration_during_work_is_audited_and_not_claimed(handoff_env):
    service = handoff_env["pipeline"].handoff_service
    handoff = create_handoff(handoff_env)
    with sqlite3.connect(handoff_env["settings"].DB_PATH) as conn:
        conn.execute(
            "UPDATE handoffs SET expires_at = 0 WHERE id = ?",
            (handoff["handoff_id"],),
        )
    result = service.claim(
        handoff["handoff_id"],
        scope=handoff_env["scope"],
        agent_id="codex",
    )
    assert result["status"] == "expired"
    assert result["event"]["event_type"] == "handoff.expired"


def test_new_checkpoint_supersedes_in_progress_handoff(handoff_env):
    service = handoff_env["pipeline"].handoff_service
    handoff = create_handoff(handoff_env)
    service.claim(handoff["handoff_id"], scope=handoff_env["scope"], agent_id="codex")
    handoff_env["pipeline"].session_service.create_checkpoint(
        handoff_env["session"]["id"],
        agent_id="claude-code",
        summary="Source state changed",
        constraints=["Do not begin Phase 6"],
        remaining=["Updated work"],
    )
    result = service.progress(
        handoff["handoff_id"],
        scope=handoff_env["scope"],
        agent_id="codex",
        progress={"completed": ["stale work"]},
    )
    assert result["status"] == "superseded"
    assert result["stale_reason"] == "source_state_changed"


def test_active_handoff_refreshes_to_new_version_when_task_changes(handoff_env):
    service = handoff_env["pipeline"].handoff_service
    first = create_handoff(handoff_env)
    handoff_env["pipeline"].task_service.update_task(
        handoff_env["task"]["id"], objective="Revised active handoff objective"
    )
    second = create_handoff(
        handoff_env,
        idempotency_key="active-refresh-2",
        expected_output="Implement the revised objective",
    )
    assert second["version"] == 2
    assert second["task"]["objective"] == "Revised active handoff objective"
    historical = service.get(
        first["handoff_id"],
        scope=handoff_env["scope"],
        version=1,
        agent_id="claude-code",
    )
    assert historical["version_status"] == "superseded"


def test_structured_completion_updates_session_and_returns_compact_delta(handoff_env):
    service = handoff_env["pipeline"].handoff_service
    handoff = create_handoff(handoff_env)
    service.claim(handoff["handoff_id"], scope=handoff_env["scope"], agent_id="codex")
    exact_skill = f"{handoff['skills'][0]['id']}@{handoff['skills'][0]['version']}"
    report = HandoffCompletion(
        files_changed=("src/recall_mcp/handoff.py",),
        work_completed=("Implemented lifecycle",),
        tests={"passed": 1, "failed": 0, "skipped": 0},
        remaining_work=("Review docs",),
        new_decisions=("Use session events for completion",),
        new_questions=("Should Phase 6 cache deltas?",),
        used_skill_versions=(exact_skill,),
        used_memory_ids=(handoff_env["memory_id"],),
    )
    completed = service.complete(
        handoff["handoff_id"],
        scope=handoff_env["scope"],
        agent_id="codex",
        completion=report,
        idempotency_key="completion-1",
    )
    replay = service.complete(
        handoff["handoff_id"],
        scope=handoff_env["scope"],
        agent_id="codex",
        completion=report,
        idempotency_key="completion-1",
    )
    assert completed["status"] == "completed"
    assert completed["checkpoint_version"] == 2
    assert replay["idempotent_replay"] is True
    checkpoint = handoff_env["pipeline"].session_service.get_checkpoint(
        handoff_env["session"]["id"]
    )
    assert checkpoint is not None
    assert "Implemented lifecycle" in checkpoint["completed"]
    assert checkpoint["approved_decisions"] == ["Use shared application services"]
    assert checkpoint["proposed_decisions"][-1]["status"] == "proposed"
    assert completed["delta"]["completion"]["tests"]["passed"] == 1
    assert "transcript" not in completed["delta"]
    with sqlite3.connect(handoff_env["settings"].DB_PATH) as conn:
        uses = conn.execute(
            "SELECT COUNT(*) FROM skill_usage WHERE usage_type = 'used'"
        ).fetchone()[0]
        memory_uses = conn.execute(
            "SELECT COUNT(*) FROM context_usage WHERE usage_type = 'used'"
        ).fetchone()[0]
    assert uses == 1
    assert memory_uses == 1


def test_completion_after_cancellation_is_rejected(handoff_env):
    service = handoff_env["pipeline"].handoff_service
    handoff = create_handoff(handoff_env)
    service.cancel(
        handoff["handoff_id"],
        scope=handoff_env["scope"],
        agent_id="claude-code",
        reason="task changed",
    )
    with pytest.raises(ContinuityError, match="cannot be completed"):
        service.complete(
            handoff["handoff_id"],
            scope=handoff_env["scope"],
            agent_id="codex",
            completion=HandoffCompletion(),
        )


def test_context_build_failure_does_not_create_partial_handoff(
    handoff_env, monkeypatch
):
    def fail_build(_request):
        raise ContinuityError("context failed", code="service_unavailable")

    monkeypatch.setattr(handoff_env["pipeline"].context_service, "build", fail_build)
    with pytest.raises(ContinuityError, match="context failed"):
        create_handoff(handoff_env)
    with sqlite3.connect(handoff_env["settings"].DB_PATH) as conn:
        assert conn.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0] == 0


def test_secondary_feedback_failure_preserves_canonical_completion(
    handoff_env, monkeypatch
):
    service = handoff_env["pipeline"].handoff_service
    handoff = create_handoff(handoff_env)
    service.claim(handoff["handoff_id"], scope=handoff_env["scope"], agent_id="codex")

    def fail_feedback(*_args, **_kwargs):
        raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr(
        handoff_env["pipeline"].skill_service, "record_usage", fail_feedback
    )
    exact = f"{handoff['skills'][0]['id']}@{handoff['skills'][0]['version']}"
    completed = service.complete(
        handoff["handoff_id"],
        scope=handoff_env["scope"],
        agent_id="codex",
        completion=HandoffCompletion(
            work_completed=("Canonical completion",),
            used_skill_versions=(exact,),
        ),
    )
    assert completed["status"] == "completed"
    assert completed["warning"]["code"] == "feedback_unavailable"
    assert (
        service.get(
            handoff["handoff_id"],
            scope=handoff_env["scope"],
            agent_id="claude-code",
        )["status"]
        == "completed"
    )


def test_historical_version_is_immutable_and_new_state_creates_version_two(
    handoff_env,
):
    service = handoff_env["pipeline"].handoff_service
    first = create_handoff(handoff_env)
    service.cancel(
        first["handoff_id"],
        scope=handoff_env["scope"],
        agent_id="claude-code",
        reason="refresh task state",
    )
    handoff_env["pipeline"].task_service.update_task(
        handoff_env["task"]["id"], objective="Implement revised handoff architecture"
    )
    second = create_handoff(
        handoff_env,
        expected_output="Revised tested implementation",
        idempotency_key="handoff-create-2",
    )
    historical = service.get(
        first["handoff_id"],
        scope=handoff_env["scope"],
        version=1,
        agent_id="claude-code",
    )
    assert second["version"] == 2
    assert historical["task"]["objective"] != second["task"]["objective"]
    assert historical["content_hash"] == first["content_hash"]
    with sqlite3.connect(handoff_env["settings"].DB_PATH) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE handoff_versions SET content_hash = 'changed' WHERE handoff_id = ? AND version = 1",
                (first["handoff_id"],),
            )


def test_explanation_renderers_reference_expansion_and_metrics(handoff_env):
    service = handoff_env["pipeline"].handoff_service
    handoff = create_handoff(handoff_env)
    explanation = service.explain(
        handoff["handoff_id"],
        scope=handoff_env["scope"],
        agent_id="codex",
    )
    assert explanation["full_transcript_required"] is False
    assert "# Task Handoff" in handoff["content_markdown"]
    assert handoff["token_estimate"] < 10_000
    with sqlite3.connect(handoff_env["settings"].DB_PATH) as conn:
        row = conn.execute(
            "SELECT id FROM handoff_references WHERE handoff_id = ? LIMIT 1",
            (handoff["handoff_id"],),
        ).fetchone()
    assert row is not None
    expanded = service.expand_reference(
        handoff["handoff_id"],
        row[0],
        scope=handoff_env["scope"],
        agent_id="codex",
    )
    assert expanded["reference_id"] == row[0]
    assert service.stats()["reference_expansions_total"] == 1


def test_claude_to_codex_to_claude_without_restatement(handoff_env):
    service = handoff_env["pipeline"].handoff_service
    handoff = create_handoff(handoff_env)
    service.claim(
        handoff["handoff_id"],
        scope=handoff_env["scope"],
        agent_id="codex",
        idempotency_key="demo-claim",
    )
    service.complete(
        handoff["handoff_id"],
        scope=handoff_env["scope"],
        agent_id="codex",
        completion=HandoffCompletion(
            files_changed=("src/recall_mcp/handoff.py",),
            work_completed=("Phase 5 implementation",),
            tests={"passed": 10, "failed": 0},
        ),
        idempotency_key="demo-complete",
    )
    source_delta = service.get_delta(
        handoff["handoff_id"],
        scope=handoff_env["scope"],
        known_version=1,
        known_checkpoint_version=1,
        agent_id="claude-code",
    )
    assert source_delta["completion"]["work_completed"] == ["Phase 5 implementation"]
    assert source_delta["session_delta"]["completed_added"] == [
        "Phase 5 implementation"
    ]
    assert source_delta["session_delta"]["to_version"] == 2
    session_event_types = {
        event["event_type"] for event in source_delta["session_delta"]["events"]
    }
    assert {"handoff.created", "handoff.claimed", "handoff.completed"} <= (
        session_event_types
    )
    full_transcript_tokens = 12_000
    assert handoff["token_estimate"] < full_transcript_tokens
    assert "Cross-project secret" not in json.dumps(source_delta)


def test_admin_inspect_and_explain_do_not_start_worker(handoff_env, capsys):
    handoff = create_handoff(handoff_env)
    scope = handoff_env["scope"]
    values = {
        "action": "inspect",
        "handoff_id": handoff["handoff_id"],
        "version": None,
        "user_id": scope.user_id,
        "workspace_id": scope.workspace_id,
        "project_id": scope.project_id,
        "repository_id": scope.repository_id,
        "task_id": scope.task_id,
        "session_id": scope.session_id,
        "agent_id": "claude-code",
        "status": None,
        "reason": None,
        "idempotency_key": None,
    }
    assert command_handoff(handoff_env["settings"], Namespace(**values)) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["content_hash"] == handoff["content_hash"]
    values["action"] = "explain"
    assert command_handoff(handoff_env["settings"], Namespace(**values)) == 0
    explained = json.loads(capsys.readouterr().out)
    assert explained["full_transcript_required"] is False


def test_handoff_benchmark_is_deterministic_and_scope_safe():
    dataset = json.loads(
        Path("tests/data/handoff_benchmark.json").read_text(encoding="utf-8")
    )
    case = dataset["cases"][0]
    required = set(case["required_sections"])
    preserved = set(case["preserved_sections"])
    mandatory_retention = len(required & preserved) / len(required)
    compression_ratio = case["handoff_token_budget"] / case["full_transcript_tokens"]
    assert mandatory_retention == 1.0
    assert compression_ratio == 0.25
    assert case["user_restatement"] is False
    assert case["rework_required"] is False
    assert case["user_corrections"] == 0
    assert (
        sum(
            case[field]
            for field in (
                "cross_user_leakage",
                "cross_project_leakage",
                "cross_repository_leakage",
                "wrong_agent_access",
            )
        )
        == 0
    )
