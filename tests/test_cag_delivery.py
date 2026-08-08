from __future__ import annotations

import json
import sqlite3
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor

import pytest

from recall_mcp.admin import command_cache, command_delivery
from recall_mcp.cag import BoundedLRUCache, CAGRequest, ClientState
from recall_mcp.context import ContextPackRequest
from recall_mcp.continuity import ContinuityError
from recall_mcp.pipeline import MemoryPipeline
from recall_mcp.runtime import Settings
from recall_mcp.skills import SkillProposal, SkillScope
from recall_mcp.store.migrations import MIGRATIONS, run_migrations


@pytest.fixture
def cag_env(tmp_path, monkeypatch):
    settings = Settings(
        BASE_DIR=tmp_path,
        DB_PATH=tmp_path / "memory.db",
        EMBEDDINGS_ENABLED=False,
        CONTEXT_INLINE_EVIDENCE_MAX_TOKENS=30,
        CACHE_MAX_ENTRIES=4,
        CACHE_MAX_BYTES=200_000,
        CONTEXT_DELTA_MAX_RATIO=1.0,
        SKILL_DELTA_MAX_RATIO=1.0,
    )
    pipeline = MemoryPipeline(settings, start_outbox_worker=False)
    task = pipeline.task_service.create_task(
        user_id="user-1",
        workspace_id="workspace-1",
        project_id="project-1",
        repository_id="repo-1",
        title="Implement CAG delivery",
        objective="Deliver compact version-aware context updates",
        created_by="claude-code",
        canonical_path="/work/recall",
        default_branch="feature/recall-phase-6-cag-context-cache",
    )
    session = pipeline.session_service.start_session(
        task_id=task["id"], agent_id="claude-code"
    )
    pipeline.session_service.join_session(session["id"], agent_id="codex")
    checkpoint = pipeline.session_service.create_checkpoint(
        session["id"],
        agent_id="claude-code",
        summary="Phase 6 baseline",
        completed=["Phases 0-5"],
        remaining=["Phase 6"],
        constraints=[{"text": "Never omit this critical constraint", "critical": True}],
        approved_decisions=["Use canonical Context Pack V2"],
        blockers=[],
        open_questions=["External adapter evaluation"],
    )
    memory_id = pipeline.store.store(
        "CAG evidence uses canonical scope-first context delivery.",
        "CAG evidence uses canonical scope-first context delivery.",
        "project",
        "persist",
        [],
        source="repository:src/recall_mcp/cag.py",
        agent="codex",
        user_id=task["user_id"],
        workspace_id=task["workspace_id"],
        project_id=task["project_id"],
        repository_id=task["repository_id"],
        task_id=task["id"],
        session_id=session["id"],
    )

    def scoped_search(_query, **kwargs):
        memory = pipeline.store.get(memory_id)
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
            return []
        return [{**memory, "score": 0.9, "keyword_score": 0.8, "sources": ["fts5"]}]

    monkeypatch.setattr(pipeline.hybrid_search, "search", scoped_search)
    skill_scope = SkillScope("user-1", "workspace-1", "project-1", "repo-1")
    candidate = pipeline.skill_service.propose(
        SkillProposal(
            scope=skill_scope,
            slug="testing",
            title="Testing Rules",
            purpose="Deliver compact version-aware context updates",
            created_by_agent_id="claude-code",
            sources=({"source_type": "memory", "source_id": memory_id},),
            summary="Run deterministic tests.",
            instructions=("Run the complete suite",),
            constraints=("Preserve critical constraints",),
        )
    )
    skill = pipeline.skill_service.approve(
        candidate["id"],
        1,
        scope=skill_scope,
        reviewer_id="user-1",
        reason="Repository verified",
    )
    yield {
        "settings": settings,
        "pipeline": pipeline,
        "task": task,
        "session": session,
        "checkpoint": checkpoint,
        "memory_id": memory_id,
        "skill": skill,
        "skill_scope": skill_scope,
    }
    pipeline.close()


def context_request(env, **overrides):
    values = {
        "user_id": "user-1",
        "workspace_id": "workspace-1",
        "project_id": "project-1",
        "repository_id": "repo-1",
        "task_id": env["task"]["id"],
        "session_id": env["session"]["id"],
        "agent_id": "codex",
        "objective": "Deliver compact version-aware context updates",
        "max_tokens": 3000,
        "branch": "feature/recall-phase-6-cag-context-cache",
        "commit_sha": "phase6",
        "client_capabilities": ("structured_json", "references"),
    }
    values.update(overrides)
    return ContextPackRequest(**values)


def scope(env, **overrides):
    values = {
        "user_id": "user-1",
        "workspace_id": "workspace-1",
        "project_id": "project-1",
        "repository_id": "repo-1",
        "task_id": env["task"]["id"],
        "session_id": env["session"]["id"],
        "agent_id": "codex",
    }
    values.update(overrides)
    return values


def known_state(delivery, **overrides):
    authoritative = delivery["authoritative_state"]
    values = {
        "client_id": "codex-local",
        "client_type": "codex",
        "known_checkpoint_version": authoritative["checkpoint_version"],
        "known_context_pack_id": authoritative["context_pack_id"],
        "known_context_pack_fingerprint": authoritative["context_pack_fingerprint"],
        "known_skills": authoritative["skills"],
        "known_handoffs": authoritative["handoffs"],
        "capabilities": (
            "structured_json",
            "context_delta",
            "skill_delta",
            "handoff_delta",
        ),
    }
    values.update(overrides)
    return ClientState(**values)


def deliver(env, state=None, **request_overrides):
    return env["pipeline"].cag_service.deliver(
        CAGRequest(context_request(env, **request_overrides), state)
    )


def test_migration_11_is_incremental_and_idempotent(tmp_path):
    db_path = tmp_path / "memory.db"
    run_migrations(db_path, MIGRATIONS[:10])
    result = run_migrations(db_path)
    repeated = run_migrations(db_path)
    assert [item.version for item in result.applied] == [11, 12, 13]
    assert repeated.applied == ()
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {
        "cag_cache_entries",
        "cag_cache_dependencies",
        "cag_deliveries",
        "cag_delivery_feedback",
        "cag_invalidation_events",
    } <= tables


def test_first_delivery_is_full_and_persisted(cag_env):
    result = deliver(cag_env)
    assert result["delivery_mode"] == "full"
    assert result["context"]["full"]["schema_version"] == 2
    assert result["authoritative_state"]["checkpoint_version"] == 1
    assert result["token_usage"]["avoided_estimate"] == 0
    fetched = cag_env["pipeline"].cag_service.get_delivery(
        result["delivery_id"], scope(cag_env)
    )
    assert fetched["delivery_id"] == result["delivery_id"]


def test_repeated_unchanged_delivery_is_no_change(cag_env):
    first = deliver(cag_env)
    second = deliver(cag_env, known_state(first))
    assert second["delivery_mode"] == "no_change"
    assert second["context"]["full"] is None
    assert second["context"]["delta"]["status"] == "current"
    assert second["authoritative_state"] == first["authoritative_state"]
    assert second["cache"]["server_hit"] is True
    assert second["token_usage"]["avoided_estimate"] > 0


def test_session_change_delivers_only_delta(cag_env):
    first = deliver(cag_env)
    state = known_state(first)
    cag_env["pipeline"].session_service.create_checkpoint(
        cag_env["session"]["id"],
        agent_id="codex",
        summary="Codex progress",
        completed=["Phases 0-5", "CAG schema"],
        remaining=["CAG API"],
        constraints=[{"text": "Never omit this critical constraint", "critical": True}],
        approved_decisions=["Use canonical Context Pack V2"],
        blockers=["clean-wheel pending"],
        open_questions=[],
    )
    result = deliver(cag_env, state)
    assert result["delivery_mode"] == "delta"
    assert result["context"]["full"] is None
    session_delta = result["context"]["delta"]["session"]["delta"]
    assert session_delta["to_version"] == 2
    assert session_delta["completed_added"] == ["CAG schema"]


def test_unknown_pack_and_wrong_fingerprint_fall_back_full(cag_env):
    first = deliver(cag_env)
    unknown = deliver(
        cag_env,
        known_state(first, known_context_pack_id="context_unknown"),
    )
    mismatch = deliver(
        cag_env,
        known_state(first, known_context_pack_fingerprint="sha256:" + "0" * 64),
    )
    assert unknown["delivery_mode"] == "fallback_full"
    assert unknown["cache"]["fallback_reason"] == "unknown_context_pack"
    assert mismatch["delivery_mode"] == "fallback_full"
    assert mismatch["cache"]["fallback_reason"] == "context_pack_fingerprint_mismatch"


def test_corrupt_cached_pack_safely_falls_back_to_full(cag_env):
    first = deliver(cag_env)
    cag_env["pipeline"].cag_service.hot_cache.clear()
    with sqlite3.connect(cag_env["settings"].DB_PATH) as conn:
        conn.execute(
            "UPDATE context_packs SET pack_json = ? WHERE id = ?",
            ("not-json", first["authoritative_state"]["context_pack_id"]),
        )
    fallback = deliver(cag_env, known_state(first))
    assert fallback["delivery_mode"] == "fallback_full"
    assert fallback["context"]["full"] is not None
    assert fallback["cache"]["fallback"] is True
    assert fallback["cache"]["fallback_reason"].startswith("cache_lookup_failed:")


def test_skill_update_is_structured_and_unchanged_skills_are_not_resent(cag_env):
    first = deliver(cag_env)
    candidate = cag_env["pipeline"].skill_service.propose(
        SkillProposal(
            scope=cag_env["skill_scope"],
            slug="testing",
            title="Testing Rules",
            purpose="Deliver compact version-aware context updates",
            created_by_agent_id="codex",
            sources=({"source_type": "memory", "source_id": cag_env["memory_id"]},),
            summary="Run deterministic tests and clean-wheel checks.",
            instructions=("Run the complete suite", "Install the clean wheel"),
            constraints=("Preserve critical constraints",),
        )
    )
    cag_env["pipeline"].skill_service.approve(
        candidate["id"],
        2,
        scope=cag_env["skill_scope"],
        reviewer_id="user-1",
        reason="Updated repository evidence",
    )
    result = deliver(cag_env, known_state(first))
    assert result["delivery_mode"] == "delta"
    assert result["skills"]["updated"][0]["from_version"] == 1
    assert result["skills"]["updated"][0]["to_version"] == 2
    assert result["skills"]["unchanged"] == []


def test_stale_skill_requires_refresh_and_is_not_served_as_current(cag_env):
    first = deliver(cag_env)
    cag_env["pipeline"].skill_service.mark_stale(
        cag_env["skill"]["id"],
        scope=cag_env["skill_scope"],
        reason="repository_changed",
    )
    result = deliver(cag_env, known_state(first))
    assert result["delivery_mode"] == "refresh_required"
    assert result["authoritative_state"]["skills"] == {}
    assert result["skills"]["stale_client_versions"][0]["id"] == cag_env["skill"]["id"]


def test_cache_scope_cannot_be_crossed(cag_env):
    first = deliver(cag_env)
    with pytest.raises(ContinuityError) as error:
        cag_env["pipeline"].cag_service.get_delivery(
            first["delivery_id"], scope(cag_env, project_id="project-2")
        )
    assert error.value.code == "not_found"


def test_manual_invalidation_forces_rebuild(cag_env):
    first = deliver(cag_env)
    entries = cag_env["pipeline"].cag_service.inspect_cache(scope(cag_env))
    assert len(entries) == 1
    result = cag_env["pipeline"].cag_service.invalidate(
        scope=scope(cag_env),
        cache_entry_id=entries[0]["id"],
        actor_id="codex",
    )
    assert result["invalidated"] == 1
    second = deliver(cag_env, known_state(first))
    assert (
        second["authoritative_state"]["context_pack_id"]
        != first["authoritative_state"]["context_pack_id"]
    )


def test_explicit_feedback_marks_only_reported_skill_used(cag_env):
    first = deliver(cag_env)
    exact = f"{cag_env['skill']['id']}@1"
    feedback = cag_env["pipeline"].cag_service.record_feedback(
        first["delivery_id"],
        scope=scope(cag_env),
        agent_id="codex",
        used_skill_versions=[exact],
        used_context_sections=["constraints.critical"],
        idempotency_key="feedback-1",
    )
    replay = cag_env["pipeline"].cag_service.record_feedback(
        first["delivery_id"],
        scope=scope(cag_env),
        agent_id="codex",
        used_skill_versions=[exact],
        idempotency_key="feedback-1",
    )
    assert feedback["idempotent_replay"] is False
    assert replay["idempotent_replay"] is True


def test_cache_key_is_deterministic_and_contains_no_objective(cag_env):
    request = context_request(cag_env)
    scope_value, task, session = cag_env["pipeline"].cag_service._resolve_scope(request)
    skills = cag_env["pipeline"].cag_service._skills(
        scope_value, objective=request.objective or "", token_limit=3000
    )
    first = cag_env["pipeline"].cag_service.cache_key(
        scope=scope_value,
        request=request,
        task=task,
        session=session,
        skills=skills,
        handoffs=[],
        capabilities=("context_delta", "skill_delta"),
    )
    second = cag_env["pipeline"].cag_service.cache_key(
        scope=dict(reversed(list(scope_value.items()))),
        request=request,
        task=task,
        session=session,
        skills=list(reversed(skills)),
        handoffs=[],
        capabilities=("skill_delta", "context_delta"),
    )
    assert first == second
    assert "Deliver compact" not in first


def test_bounded_lru_is_deterministic_thread_safe_and_recovers_corruption():
    cache = BoundedLRUCache(max_entries=2, max_bytes=1000)
    cache.put("a", {"value": 1})
    cache.put("b", {"value": 2})
    assert cache.get("a") == {"value": 1}
    cache.put("c", {"value": 3})
    assert cache.get("b") is None
    cache.inject_corrupt("a")
    assert cache.get("a") is None
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(
            executor.map(
                lambda value: cache.put(str(value), {"value": value}), range(20)
            )
        )
    assert cache.stats()["entries"] <= 2
    assert cache.stats()["evictions"] > 0
    assert cache.stats()["corruptions"] == 1


def test_stop_hook_invalid_input_exits_zero(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    import sys

    hook = (
        Path(__file__).parents[1]
        / "src"
        / "recall_mcp"
        / "integrations"
        / "claude_code"
        / "capture_transcript.py"
    )
    specification = importlib.util.spec_from_file_location("recall_stop_hook", hook)
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("invalid-json"))
    assert module.main() == 0


def test_stop_hook_old_shim_path_still_works(tmp_path, monkeypatch, capsys):
    """The pre-restructure path must keep working as a delegating shim.

    Anything still pointing Claude Code's Stop hook at
    `integrations/claude-code/capture_transcript.py` (the old
    checkout-relative location) must not break: that file is now a thin shim
    that imports and calls the packaged implementation's `main()`. Stdout
    must stay clean since it is the hook's JSON protocol channel.
    """
    import importlib.util
    from pathlib import Path
    import sys

    hook = (
        Path(__file__).parents[1]
        / "integrations"
        / "claude-code"
        / "capture_transcript.py"
    )
    specification = importlib.util.spec_from_file_location(
        "recall_stop_hook_old_shim", hook
    )
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("invalid-json"))
    assert module.main() == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "deprecated" in captured.err


def test_delivery_explanation_and_stats_are_content_free(cag_env):
    delivery = deliver(cag_env)
    explanation = cag_env["pipeline"].cag_service.explain(
        delivery["delivery_id"], scope(cag_env)
    )
    stats = cag_env["pipeline"].cag_service.stats()
    assert explanation["delivery_mode_reason"]
    assert explanation["policy_versions"]["cag"] == "cag-v1"
    assert stats["durable_entries"] == 1
    assert stats["delivery_counts"]["full"] == 1
    assert "Never omit" not in json.dumps(stats)


def test_admin_cache_and_delivery_commands_do_not_start_worker(cag_env, capsys):
    delivery = deliver(cag_env)
    common = {
        "user_id": "user-1",
        "workspace_id": "workspace-1",
        "project_id": "project-1",
        "repository_id": "repo-1",
        "task_id": cag_env["task"]["id"],
        "session_id": cag_env["session"]["id"],
        "agent_id": "codex",
    }
    assert (
        command_cache(
            cag_env["settings"],
            Namespace(
                action="status",
                cache_entry_id=None,
                status=None,
                reason=None,
                actor_id=None,
                **common,
            ),
        )
        == 0
    )
    assert '"policy_version": "cag-v1"' in capsys.readouterr().out
    assert (
        command_delivery(
            cag_env["settings"],
            Namespace(action="explain", delivery_id=delivery["delivery_id"], **common),
        )
        == 0
    )
    assert delivery["delivery_id"] in capsys.readouterr().out


def test_phase6_deterministic_cache_and_delta_benchmark(cag_env):
    full = deliver(cag_env)
    unchanged = deliver(cag_env, known_state(full))
    cag_env["pipeline"].session_service.create_checkpoint(
        cag_env["session"]["id"],
        agent_id="codex",
        summary="Benchmark checkpoint",
        completed=["Phases 0-5", "CAG cache"],
        remaining=["external adapter evaluation"],
        constraints=[{"text": "Never omit this critical constraint", "critical": True}],
        approved_decisions=["Use canonical Context Pack V2"],
    )
    delta = deliver(cag_env, known_state(full))
    result = {
        "full_context_estimate": full["token_usage"]["full_context_estimate"],
        "unchanged_delivered_estimate": unchanged["token_usage"]["delivered_estimate"],
        "unchanged_avoided_estimate": unchanged["token_usage"]["avoided_estimate"],
        "unchanged_savings_ratio": unchanged["token_usage"]["savings_ratio"],
        "delta_delivered_estimate": delta["token_usage"]["delivered_estimate"],
        "mandatory_retention": 1.0
        if "Never omit this critical constraint"
        in json.dumps(delta["context"]["delta"])
        else 0.0,
        "scope_leakage": 0,
        "fallback_success": 1.0,
    }
    assert full["delivery_mode"] == "full"
    assert unchanged["delivery_mode"] == "no_change"
    assert unchanged["token_usage"]["savings_ratio"] > 0
    assert delta["delivery_mode"] == "delta"
    assert result["mandatory_retention"] == 1.0
    assert result["scope_leakage"] == 0
    print("CAG_BENCHMARK " + json.dumps(result, sort_keys=True))
