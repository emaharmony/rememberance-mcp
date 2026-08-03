from __future__ import annotations

import json
import sqlite3
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from recall_mcp.context import ContextPackRequest
from recall_mcp.admin import command_skill
from recall_mcp.continuity import ContinuityError
from recall_mcp.pipeline import MemoryPipeline
from recall_mcp.runtime import Settings
from recall_mcp.skills import (
    SkillCandidateService,
    SkillProposal,
    SkillScope,
    SkillService,
)
from recall_mcp.store.migrations import MIGRATIONS, run_migrations


@pytest.fixture
def skill_env(tmp_path):
    settings = Settings(
        BASE_DIR=tmp_path,
        DB_PATH=tmp_path / "memory.db",
        EMBEDDINGS_ENABLED=False,
    )
    pipeline = MemoryPipeline(settings, start_outbox_worker=False)
    task = pipeline.task_service.create_task(
        user_id="user-1",
        workspace_id="workspace-1",
        project_id="project-1",
        repository_id="repo-1",
        title="Version Recall Skills",
        objective="Compile stable architecture knowledge",
        created_by="claude-code",
        canonical_path="/work/recall",
    )
    memory_id = pipeline.store.store(
        "Scope filtering happens before ranking.",
        "Scope filtering happens before ranking.",
        "decision",
        "persist",
        [],
        source="repository:src/recall_mcp/context.py",
        agent="claude-code",
        user_id=task["user_id"],
        workspace_id=task["workspace_id"],
        project_id=task["project_id"],
        repository_id=task["repository_id"],
        task_id=task["id"],
    )
    service = SkillService(settings.DB_PATH, settings)
    yield {
        "settings": settings,
        "pipeline": pipeline,
        "task": task,
        "memory_id": memory_id,
        "service": service,
        "scope": SkillScope("user-1", "workspace-1", "project-1", "repo-1"),
    }
    pipeline.close()


def proposal(env, **overrides):
    values = {
        "scope": env["scope"],
        "slug": "architecture",
        "title": "Recall Architecture",
        "purpose": "Preserve stable architectural guidance",
        "created_by_agent_id": "claude-code",
        "sources": (
            {
                "source_type": "memory",
                "source_id": env["memory_id"],
                "relationship": "supports",
            },
        ),
        "summary": "Recall keeps scope enforcement ahead of ranking.",
        "instructions": ("Use shared application services",),
        "constraints": ("Do not bypass the outbox",),
    }
    values.update(overrides)
    return SkillProposal(**values)


def test_migration_9_is_incremental_idempotent_and_adds_immutable_tables(tmp_path):
    db_path = tmp_path / "memory.db"
    run_migrations(db_path, MIGRATIONS[:8])
    upgraded = run_migrations(db_path, MIGRATIONS[:9])
    repeated = run_migrations(db_path, MIGRATIONS[:9])

    assert [item.version for item in upgraded.applied] == [9]
    assert repeated.from_version == repeated.to_version == 9
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
    assert {
        "skills",
        "skill_versions",
        "skill_sources",
        "skill_reviews",
        "skill_usage",
        "context_pack_skills",
    } <= tables
    assert {
        "skill_versions_no_update",
        "skill_versions_no_delete",
        "skill_sources_no_update",
        "skill_reviews_no_update",
    } <= triggers


def test_candidate_from_memory_is_deterministic_idempotent_and_renderable(skill_env):
    service = skill_env["service"]
    candidate_service = SkillCandidateService(service)
    first = candidate_service.propose_from_memories(
        proposal(skill_env, idempotency_key="skill-create-1")
    )
    replay = candidate_service.propose_from_memories(
        proposal(skill_env, idempotency_key="skill-create-1")
    )

    assert first["version"] == 1
    assert first["status"] == "pending_approval"
    assert first["version_status"] == "pending_approval"
    assert replay["id"] == first["id"]
    assert replay["idempotent_replay"] is True
    assert replay["content_hash"] == first["content_hash"]
    assert replay["source_fingerprint"] == first["source_fingerprint"]
    assert (
        service.render_json(first["id"], scope=skill_env["scope"])["content_hash"]
        == first["content_hash"]
    )
    markdown = service.render_markdown(first["id"], scope=skill_env["scope"])
    assert "# Recall Architecture" in markdown
    assert "## Evidence" in markdown
    assert first["token_estimate"] > 0


def test_exact_version_approval_and_history_remain_immutable(skill_env):
    service = skill_env["service"]
    version_one = service.propose(proposal(skill_env))
    approved = service.approve(
        version_one["id"],
        1,
        scope=skill_env["scope"],
        reviewer_id="user-1",
        reason="Repository evidence verified",
        idempotency_key="approve-1",
    )
    replay = service.approve(
        version_one["id"],
        1,
        scope=skill_env["scope"],
        reviewer_id="user-1",
        reason="Repository evidence verified",
        idempotency_key="approve-1",
    )
    version_two = service.propose(
        proposal(skill_env, instructions=("Use the shared service", "Keep provenance"))
    )

    assert approved["version_status"] == "approved"
    assert replay["idempotent_replay"] is True
    assert version_two["version"] == 2
    assert version_two["version_status"] == "pending_approval"
    assert service.get(version_one["id"], scope=skill_env["scope"])["version"] == 1
    versions = service.versions(version_one["id"], scope=skill_env["scope"])
    assert [item["version"] for item in versions] == [1, 2]
    assert versions[0]["content_hash"] == version_one["content_hash"]
    with sqlite3.connect(skill_env["settings"].DB_PATH) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE skill_versions SET content_json = '{}' WHERE skill_id = ? AND version = 1",
                (version_one["id"],),
            )


def test_rejection_authorization_and_contradictions_are_auditable(skill_env):
    service = skill_env["service"]
    candidate = service.propose(
        proposal(
            skill_env,
            sources=(
                {
                    "source_type": "memory",
                    "source_id": skill_env["memory_id"],
                    "relationship": "contradicts",
                },
            ),
        )
    )
    with pytest.raises(ContinuityError, match="not authorized"):
        service.approve(
            candidate["id"],
            1,
            scope=skill_env["scope"],
            reviewer_id="codex",
        )
    rejected = service.reject(
        candidate["id"],
        1,
        scope=skill_env["scope"],
        reviewer_id="user-1",
        reason="Contradiction needs resolution",
    )
    explanation = service.explain(candidate["id"], scope=skill_env["scope"])
    assert rejected["version_status"] == "rejected"
    assert explanation["contradictions"][0]["source_id"] == skill_env["memory_id"]
    with pytest.raises(ContinuityError, match="final review"):
        service.approve(
            candidate["id"],
            1,
            scope=skill_env["scope"],
            reviewer_id="user-1",
        )


def test_scope_is_enforced_before_source_lookup_and_skill_selection(skill_env):
    pipeline = skill_env["pipeline"]
    other = pipeline.task_service.create_task(
        user_id="user-1",
        workspace_id="workspace-1",
        project_id="project-2",
        repository_id="repo-2",
        title="Other project",
        objective="Distractor",
        created_by="claude-code",
        canonical_path="/work/other",
    )
    other_memory = pipeline.store.store(
        "Cross-project private guidance",
        "Cross-project private guidance",
        "project",
        "persist",
        [],
        user_id=other["user_id"],
        workspace_id=other["workspace_id"],
        project_id=other["project_id"],
        repository_id=other["repository_id"],
        task_id=other["id"],
    )
    with pytest.raises(ContinuityError, match="not found in scope"):
        skill_env["service"].propose(
            proposal(
                skill_env,
                sources=({"source_type": "memory", "source_id": other_memory},),
            )
        )

    candidate = skill_env["service"].propose(proposal(skill_env))
    with pytest.raises(ContinuityError, match="not found"):
        skill_env["service"].get(
            candidate["id"],
            scope=SkillScope("user-1", "workspace-1", "project-2", "repo-2"),
        )


def test_approved_selection_usage_and_stale_refresh(skill_env):
    service = skill_env["service"]
    candidate = service.propose(proposal(skill_env))
    assert (
        service.select_for_context(
            scope=skill_env["scope"], objective="architecture", token_limit=1000
        )
        == []
    )
    service.approve(candidate["id"], 1, scope=skill_env["scope"], reviewer_id="user-1")
    selected = service.select_for_context(
        scope=skill_env["scope"], objective="architecture ranking", token_limit=1000
    )
    assert [(item["id"], item["version"]) for item in selected] == [
        (candidate["id"], 1)
    ]
    usage = service.record_usage(
        candidate["id"],
        1,
        scope=skill_env["scope"],
        usage_type="used",
        agent_id="codex",
        idempotency_key="used-1",
    )
    assert usage["idempotent_replay"] is False
    assert (
        service.record_usage(
            candidate["id"],
            1,
            scope=skill_env["scope"],
            usage_type="used",
            agent_id="codex",
            idempotency_key="used-1",
        )["idempotent_replay"]
        is True
    )

    with sqlite3.connect(skill_env["settings"].DB_PATH) as conn:
        conn.execute(
            "UPDATE memories SET content = ?, summary = ? WHERE id = ?",
            (
                "Scope and authorization happen before ranking.",
                "Scope and authorization happen before ranking.",
                skill_env["memory_id"],
            ),
        )
    refreshed = service.refresh(
        candidate["id"],
        scope=skill_env["scope"],
        created_by_agent_id="claude-code",
    )
    assert refreshed["status"] == "stale"
    assert refreshed["candidate_version"]["version"] == 2
    assert (
        service.select_for_context(
            scope=skill_env["scope"], objective="architecture", token_limit=1000
        )
        == []
    )
    assert (
        service.get(candidate["id"], scope=skill_env["scope"], version=1)[
            "content_hash"
        ]
        == candidate["content_hash"]
    )


def test_concurrent_candidate_creation_allocates_one_stable_version(skill_env):
    service = skill_env["service"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(lambda _: service.propose(proposal(skill_env)), range(2))
        )

    assert {result["version"] for result in results} == {1}
    assert sum(bool(result["idempotent_replay"]) for result in results) == 1


def test_repository_candidate_requires_verified_scoped_content(skill_env):
    candidate = SkillCandidateService(
        skill_env["service"]
    ).propose_from_repository_evidence(
        proposal(
            skill_env,
            slug="repository-rules",
            sources=(
                {
                    "source_type": "repository",
                    "source_id": "repo-1",
                    "source_ref": "src/recall_mcp/context.py",
                    "content": "Context Pack V2 is the canonical delivery service.",
                    "commit_sha": "abc123",
                },
            ),
        )
    )
    assert candidate["content"]["facts"][0]["verification"] == ("repository_verified")
    with pytest.raises(ContinuityError, match="source_ref and content"):
        SkillCandidateService(skill_env["service"]).propose_from_repository_evidence(
            proposal(
                skill_env,
                slug="missing-evidence",
                sources=({"source_type": "repository", "source_id": "repo-1"},),
            )
        )


def test_context_pack_selects_exact_approved_skill_and_feedback_marks_actual_use(
    skill_env,
):
    service = skill_env["service"]
    candidate = service.propose(proposal(skill_env))
    service.approve(candidate["id"], 1, scope=skill_env["scope"], reviewer_id="user-1")
    skill_env["settings"].SKILL_CONTEXT_BUDGET_RATIO = 0.0
    task = skill_env["task"]
    pack = skill_env["pipeline"].build_context_v2(
        ContextPackRequest(
            user_id=task["user_id"],
            workspace_id=task["workspace_id"],
            project_id=task["project_id"],
            repository_id=task["repository_id"],
            task_id=task["id"],
            agent_id="codex",
            objective="Continue the Recall architecture implementation",
            max_tokens=3000,
            client_capabilities=("structured_json", "skill_references"),
        )
    )

    assert [(item["id"], item["version"]) for item in pack["skills"]] == [
        (candidate["id"], 1)
    ]
    assert pack["skills"][0]["delivery"] == "reference"
    skill_reference = next(
        item for item in pack["references"] if item["type"] == "skill"
    )
    with sqlite3.connect(skill_env["settings"].DB_PATH) as conn:
        usages = conn.execute(
            """
            SELECT usage_type FROM skill_usage
            WHERE context_pack_id = ? ORDER BY usage_type
            """,
            (pack["context_pack_id"],),
        ).fetchall()
        linkage = conn.execute(
            """
            SELECT skill_id, skill_version, disposition FROM context_pack_skills
            WHERE context_pack_id = ?
            """,
            (pack["context_pack_id"],),
        ).fetchone()
    assert [row[0] for row in usages] == ["referenced", "selected"]
    assert linkage == (candidate["id"], 1, "reference")

    expanded = skill_env["pipeline"].context_service.expand_reference(
        pack["context_pack_id"],
        skill_reference["reference_id"],
        {
            "user_id": task["user_id"],
            "workspace_id": task["workspace_id"],
            "project_id": task["project_id"],
            "repository_id": task["repository_id"],
            "task_id": task["id"],
        },
        agent_id="codex",
    )
    assert expanded["skill_id"] == candidate["id"]
    feedback = skill_env["pipeline"].context_service.record_feedback(
        context_pack_id=pack["context_pack_id"],
        scope={
            "user_id": task["user_id"],
            "workspace_id": task["workspace_id"],
            "project_id": task["project_id"],
            "repository_id": task["repository_id"],
            "task_id": task["id"],
        },
        agent_id="codex",
        used_skill_versions=({"id": candidate["id"], "version": 1},),
        idempotency_key="skill-feedback-1",
    )
    assert any(item.get("usage_type") == "used" for item in feedback)


def test_context_pack_warns_and_excludes_stale_skill(skill_env):
    service = skill_env["service"]
    candidate = service.propose(proposal(skill_env))
    service.approve(candidate["id"], 1, scope=skill_env["scope"], reviewer_id="user-1")
    service.mark_stale(candidate["id"], scope=skill_env["scope"])
    task = skill_env["task"]
    pack = skill_env["pipeline"].build_context_v2(
        ContextPackRequest(
            user_id=task["user_id"],
            workspace_id=task["workspace_id"],
            project_id=task["project_id"],
            repository_id=task["repository_id"],
            task_id=task["id"],
            agent_id="codex",
            objective="Recall architecture",
            max_tokens=3000,
        )
    )
    assert pack["skills"] == []
    assert "stale_skills_excluded" in {item["code"] for item in pack["warnings"]}


def test_admin_skill_inspection_and_review_do_not_start_worker(skill_env, capsys):
    candidate = skill_env["service"].propose(proposal(skill_env))
    values = {
        "action": "inspect",
        "skill_id": candidate["id"],
        "version": 1,
        "user_id": "user-1",
        "workspace_id": "workspace-1",
        "project_id": "project-1",
        "repository_id": "repo-1",
        "status": None,
        "slug": None,
        "title": None,
        "purpose": None,
        "summary": None,
        "agent_id": None,
        "reviewer_id": None,
        "reason": None,
        "memory_id": None,
        "source_ref": None,
        "source_content": None,
    }
    assert command_skill(skill_env["settings"], Namespace(**values)) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["content_hash"] == candidate["content_hash"]

    values.update(
        action="approve", reviewer_id="user-1", reason="admin review complete"
    )
    assert command_skill(skill_env["settings"], Namespace(**values)) == 0
    approved = json.loads(capsys.readouterr().out)
    assert approved["version_status"] == "approved"


def test_phase4_end_to_end_repeated_context_approval_use_and_stale_refresh(skill_env):
    pipeline = skill_env["pipeline"]
    task = skill_env["task"]
    pack_ids = []
    for index, agent_id in enumerate(("codex", "claude-code"), start=1):
        pack = pipeline.build_context_v2(
            ContextPackRequest(
                user_id=task["user_id"],
                workspace_id=task["workspace_id"],
                project_id=task["project_id"],
                repository_id=task["repository_id"],
                task_id=task["id"],
                agent_id=agent_id,
                objective="Scope filtering happens before ranking",
                max_tokens=3000,
                idempotency_key=f"phase4-source-pack-{index}",
            )
        )
        pack_ids.append(pack["context_pack_id"])
        pipeline.context_service.record_feedback(
            context_pack_id=pack["context_pack_id"],
            scope={
                "user_id": task["user_id"],
                "workspace_id": task["workspace_id"],
                "project_id": task["project_id"],
                "repository_id": task["repository_id"],
                "task_id": task["id"],
            },
            agent_id=agent_id,
            used_memory_ids=(skill_env["memory_id"],),
            idempotency_key=f"phase4-source-used-{index}",
        )

    sources = tuple(
        {"source_type": "context_pack", "source_id": pack_id} for pack_id in pack_ids
    ) + (
        {
            "source_type": "repository",
            "source_id": task["repository_id"],
            "source_ref": "src/recall_mcp/context.py",
            "statement": "Context Pack V2 is the canonical context service.",
            "content": "Context Pack V2 delegates all transports to one shared service. "
            * 30,
            "commit_sha": "phase4-v1",
        },
    )
    version_one = SkillCandidateService(skill_env["service"]).skills.propose(
        proposal(
            skill_env,
            slug="phase4-architecture",
            sources=sources,
            summary="Use one scope-first provider-neutral context service.",
        )
    )
    skill_env["service"].approve(
        version_one["id"],
        1,
        scope=skill_env["scope"],
        reviewer_id="user-1",
    )
    pipeline.settings.SKILL_CONTEXT_BUDGET_RATIO = 0.0
    delivered = pipeline.build_context_v2(
        ContextPackRequest(
            user_id=task["user_id"],
            workspace_id=task["workspace_id"],
            project_id=task["project_id"],
            repository_id=task["repository_id"],
            task_id=task["id"],
            agent_id="codex",
            objective="Continue Recall architecture work",
            max_tokens=3000,
            client_capabilities=("skill_references",),
        )
    )
    selected = next(
        item for item in delivered["skills"] if item["id"] == version_one["id"]
    )
    assert selected["version"] == 1
    reference = next(
        item
        for item in delivered["references"]
        if item["type"] == "skill" and item["source"]["skill_id"] == version_one["id"]
    )
    pipeline.context_service.expand_reference(
        delivered["context_pack_id"],
        reference["reference_id"],
        {
            "user_id": task["user_id"],
            "workspace_id": task["workspace_id"],
            "project_id": task["project_id"],
            "repository_id": task["repository_id"],
            "task_id": task["id"],
        },
        agent_id="codex",
    )
    skill_env["service"].record_usage(
        version_one["id"],
        1,
        scope=skill_env["scope"],
        usage_type="used",
        agent_id="codex",
        context_pack_id=delivered["context_pack_id"],
        task_id=task["id"],
        idempotency_key="phase4-skill-used",
    )

    skill_env["service"].mark_stale(
        version_one["id"],
        scope=skill_env["scope"],
        reason="repository_changed",
    )
    version_two_sources = (
        *sources[:-1],
        {
            **sources[-1],
            "commit_sha": "phase4-v2",
            "content": "Context Pack V2 remains canonical and now includes approved skills. "
            * 30,
        },
    )
    version_two = skill_env["service"].propose(
        proposal(
            skill_env,
            slug="phase4-architecture",
            sources=version_two_sources,
            summary="Use one scope-first context service with approved skills.",
        )
    )
    history = skill_env["service"].versions(version_one["id"], scope=skill_env["scope"])
    assert version_two["version"] == 2
    assert version_two["version_status"] == "pending_approval"
    assert history[0]["content_hash"] == version_one["content_hash"]
    assert history[0]["version_status"] == "approved"
    raw_tokens = len(str(sources[-1]["content"]).encode("utf-8")) / 4
    assert version_one["token_estimate"] < raw_tokens


def test_skill_benchmark_dataset_is_deterministic():
    dataset = json.loads(
        Path("tests/data/recall_skills_benchmark.json").read_text(encoding="utf-8")
    )
    candidates = [case for case in dataset["cases"] if case["candidate"]]
    false_candidates = [
        case
        for case in candidates
        if case["reason"]
        in {"user correction", "scope mismatch", "unresolved contradiction"}
    ]
    precision = (len(candidates) - len(false_candidates)) / len(candidates)

    assert precision == dataset["expected"]["candidate_precision"] == 1.0
    assert {case["id"] for case in dataset["cases"]} >= {
        "corrected-frequent",
        "unverified-agent",
        "stale-summary",
        "cross-project",
        "contradicted-fact",
        "pinned-constraint",
    }
    assert dataset["expected"]["cross_project_leakage"] == 0
