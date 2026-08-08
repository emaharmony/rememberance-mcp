from __future__ import annotations

import json
import sqlite3
from argparse import Namespace

import pytest

from recall_mcp.admin import command_context
from recall_mcp.context import ContextPackRequest, TokenEstimator
from recall_mcp.continuity import ContinuityError
from recall_mcp.pipeline import MemoryPipeline
from recall_mcp.runtime import Settings


@pytest.fixture
def context_env(tmp_path, monkeypatch):
    settings = Settings(
        BASE_DIR=tmp_path,
        DB_PATH=tmp_path / "memory.db",
        EMBEDDINGS_ENABLED=False,
        CONTEXT_INLINE_EVIDENCE_MAX_TOKENS=30,
    )
    pipeline = MemoryPipeline(settings, start_outbox_worker=False)
    task = pipeline.task_service.create_task(
        user_id="user-1",
        workspace_id="workspace-1",
        project_id="project-1",
        repository_id="repo-1",
        title="Implement Context Pack V2",
        objective="Continue Phase 3 implementation",
        created_by="claude-code",
        canonical_path="/work/recall",
        default_branch="feature/context-v2",
    )
    session = pipeline.session_service.start_session(
        task_id=task["id"], agent_id="claude-code"
    )
    pipeline.session_service.join_session(session["id"], agent_id="codex")
    pipeline.session_service.create_checkpoint(
        session["id"],
        agent_id="claude-code",
        summary="Phase 3 checkpoint",
        completed=["schema"],
        remaining=["API", "tests"],
        constraints=["no push", {"text": "keep V1", "critical": True}],
        approved_decisions=["use one shared context service"],
        proposed_decisions=["enable provider tokenizers"],
        open_questions=["external Prism adapter"],
        blockers=["clean-wheel gate"],
        important_files=["src/recall_mcp/context.py"],
        known_failures=["Ollama scout OOM"],
    )
    small_id = pipeline.store.store(
        "Use the shared context service.",
        "Use the shared context service.",
        "decision",
        "active",
        [],
        source="repository:src/recall_mcp/context.py",
        agent="claude-code",
        user_id=task["user_id"],
        workspace_id=task["workspace_id"],
        project_id=task["project_id"],
        repository_id=task["repository_id"],
        task_id=task["id"],
        session_id=session["id"],
    )
    long_text = "Successful test output proves the allocator path. " * 60
    long_id = pipeline.store.store(
        long_text,
        long_text,
        "tool_result",
        "active",
        [],
        source="test:pytest-context-pack",
        agent="codex",
        user_id=task["user_id"],
        workspace_id=task["workspace_id"],
        project_id=task["project_id"],
        repository_id=task["repository_id"],
        task_id=task["id"],
        session_id=session["id"],
    )
    other_task = pipeline.task_service.create_task(
        user_id="user-1",
        workspace_id="workspace-1",
        project_id="project-2",
        repository_id="repo-2",
        title="Distractor",
        objective="unrelated work",
        created_by="claude-code",
        canonical_path="/work/other",
    )
    other_session = pipeline.session_service.start_session(
        task_id=other_task["id"], agent_id="claude-code"
    )
    other_id = pipeline.store.store(
        "Cross-project secret distractor.",
        "Cross-project secret distractor.",
        "project",
        "active",
        [],
        agent="claude-code",
        user_id=other_task["user_id"],
        workspace_id=other_task["workspace_id"],
        project_id=other_task["project_id"],
        repository_id=other_task["repository_id"],
        task_id=other_task["id"],
        session_id=other_session["id"],
    )
    candidate_ids = [small_id, long_id, other_id]
    search_calls: list[dict[str, object]] = []

    def scoped_search(_query, **kwargs):
        search_calls.append(dict(kwargs))
        results = []
        for memory_id in candidate_ids:
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
                continue
            results.append(
                {
                    **memory,
                    "score": 0.9 if memory_id == small_id else 0.8,
                    "keyword_score": 0.7,
                    "sources": ["fts5"],
                }
            )
        return results[: int(kwargs["limit"])]

    monkeypatch.setattr(pipeline.hybrid_search, "search", scoped_search)
    yield {
        "settings": settings,
        "pipeline": pipeline,
        "task": task,
        "session": session,
        "small_id": small_id,
        "long_id": long_id,
        "other_id": other_id,
        "other_task": other_task,
        "other_session": other_session,
        "search_calls": search_calls,
    }
    pipeline.close()


def request(env, **overrides):
    values = {
        "user_id": "user-1",
        "workspace_id": "workspace-1",
        "project_id": "project-1",
        "repository_id": "repo-1",
        "task_id": env["task"]["id"],
        "session_id": env["session"]["id"],
        "agent_id": "codex",
        "max_tokens": 3000,
        "known_checkpoint_version": 0,
        "branch": "feature/context-v2",
        "commit_sha": "abc123",
        "client_capabilities": ("structured_json", "references", "session_delta"),
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
    }
    values.update(overrides)
    return values


def test_builds_complete_pack_and_filters_scope_before_ranking(context_env):
    pack = context_env["pipeline"].build_context_v2(request(context_env))

    assert pack["schema_version"] == 2
    assert pack["policy_version"] == "context-v2"
    assert pack["active_task"]["objective"] == "Continue Phase 3 implementation"
    assert pack["decisions"]["approved"][0]["trust"]["authority"] == (
        "user_approved_decision"
    )
    assert {
        item["value"] if isinstance(item["value"], str) else item["value"]["text"]
        for item in pack["constraints"]["critical"]
    } == {"no push", "keep V1"}
    assert pack["work_state"]["blockers"] == ["clean-wheel gate"]
    assert pack["session"]["delta"]["status"] == "changed"
    assert any(
        change["agent_id"] == "claude-code"
        for change in pack["session"]["delta"]["changes"]
    )
    assert context_env["other_id"] not in {
        item["id"] for item in pack["retrieval"]["results"]
    }
    call = context_env["search_calls"][0]
    assert call["project_id"] == "project-1"
    assert call["repository_id"] == "repo-1"
    assert call["task_id"] == context_env["task"]["id"]
    assert "transcript" not in json.dumps(pack).lower()


@pytest.mark.parametrize("missing", ["user_id", "project_id", "task_id"])
def test_request_validation_requires_formal_scope(context_env, missing):
    with pytest.raises(ContinuityError) as captured:
        context_env["pipeline"].build_context_v2(
            request(context_env, **{missing: None})
        )
    assert captured.value.code == "missing_scope"


def test_rejects_cross_repository_and_cross_task_session(context_env):
    with pytest.raises(ContinuityError) as repository_error:
        context_env["pipeline"].build_context_v2(
            request(context_env, repository_id="repo-2")
        )
    assert repository_error.value.code == "not_found"

    with pytest.raises(ContinuityError) as session_error:
        context_env["pipeline"].build_context_v2(
            request(context_env, session_id=context_env["other_session"]["id"])
        )
    assert session_error.value.code == "scope_mismatch"


def test_defensive_scope_check_rejects_bad_retrieval_adapter(context_env):
    pipeline = context_env["pipeline"]
    distractor = pipeline.store.get(context_env["other_id"])
    assert distractor is not None
    original = pipeline.context_service.search
    pipeline.context_service.search = lambda **_kwargs: (
        [{**distractor, "score": 1.0, "sources": ["bad-adapter"]}],
        "bad-run",
    )
    try:
        with pytest.raises(ContinuityError) as rejected:
            pipeline.build_context_v2(request(context_env))
    finally:
        pipeline.context_service.search = original
    assert rejected.value.code == "scope_mismatch"


def test_session_delta_current_missing_and_invalid_versions_are_safe(context_env):
    pipeline = context_env["pipeline"]
    current = pipeline.build_context_v2(
        request(context_env, known_checkpoint_version=1)
    )
    assert current["session"]["delta"]["status"] == "current"
    assert current["session"]["delta"]["changes"] == []

    checkpoint = pipeline.build_context_v2(
        request(context_env, known_checkpoint_version=None)
    )
    assert checkpoint["session"]["delta"]["status"] == "checkpoint"
    assert checkpoint["session"]["delta"]["changes"] == []

    future = pipeline.build_context_v2(
        request(context_env, known_checkpoint_version=999)
    )
    assert future["session"]["delta"]["status"] == "safe_checkpoint"
    assert any(warning["code"] == "invalid_version" for warning in future["warnings"])


def test_closed_session_remains_available_without_transcript_replay(context_env):
    pipeline = context_env["pipeline"]
    pipeline.session_service.close_session(
        context_env["session"]["id"], agent_id="claude-code"
    )
    pack = pipeline.build_context_v2(request(context_env, known_checkpoint_version=1))
    assert pack["session"]["status"] == "closed"
    assert pack["session"]["checkpoint"]["version"] == 1
    assert pack["session"]["delta"]["status"] == "changed"
    assert "transcript" not in pack


def test_tight_budget_preserves_critical_context_and_reports_conflict(context_env):
    pack = context_env["pipeline"].build_context_v2(
        request(context_env, max_tokens=10, client_capabilities=("structured_json",))
    )
    assert "no push" in pack["inline_context"]
    assert pack["constraints"]["critical"]
    assert pack["token_usage"]["estimated_total"] > 10
    assert any(
        warning["code"] == "mandatory_budget_exceeded" for warning in pack["warnings"]
    )
    assert pack["retrieval"]["selected_memory_ids"] == []


def test_reference_expansion_is_scoped_and_not_marked_used(context_env):
    pack = context_env["pipeline"].build_context_v2(request(context_env))
    reference = next(
        item
        for item in pack["references"]
        if item["memory_id"] == context_env["long_id"]
    )
    expanded = context_env["pipeline"].context_service.expand_reference(
        pack["context_pack_id"],
        reference["reference_id"],
        scope(context_env),
        agent_id="codex",
    )
    assert "Successful test output" in expanded["content"]
    with sqlite3.connect(context_env["settings"].DB_PATH) as conn:
        usages = {
            row[0]
            for row in conn.execute(
                "SELECT usage_type FROM context_usage WHERE memory_id = ?",
                (context_env["long_id"],),
            )
        }
        flags = conn.execute(
            """
            SELECT selected, injected, used FROM retrieval_results
            WHERE retrieval_run_id = ? AND memory_id = ?
            """,
            (pack["retrieval"]["retrieval_run_id"], context_env["long_id"]),
        ).fetchone()
    assert "expanded" in usages
    assert "used" not in usages
    assert flags == (1, 0, 0)
    with pytest.raises(ContinuityError):
        context_env["pipeline"].context_service.expand_reference(
            pack["context_pack_id"],
            reference["reference_id"],
            scope(context_env, project_id="project-2"),
            agent_id="codex",
        )


def test_pack_idempotency_fingerprint_and_explanation_are_stable(context_env):
    pipeline = context_env["pipeline"]
    first = pipeline.build_context_v2(
        request(context_env, idempotency_key="context-build-1")
    )
    replay = pipeline.build_context_v2(
        request(context_env, idempotency_key="context-build-1")
    )
    assert replay["context_pack_id"] == first["context_pack_id"]
    assert replay["idempotent_replay"] is True
    with pytest.raises(ContinuityError) as conflict:
        pipeline.build_context_v2(
            request(context_env, idempotency_key="context-build-1", max_tokens=2999)
        )
    assert conflict.value.code == "idempotency_conflict"

    second = pipeline.build_context_v2(request(context_env))
    assert (
        second["freshness"]["source_fingerprint"]
        == first["freshness"]["source_fingerprint"]
    )
    explanation = pipeline.context_service.explain(
        first["context_pack_id"], scope(context_env)
    )
    assert explanation["utility_affected_ranking"] is False
    assert explanation["policy_versions"]["token_estimator"] == "chars-v1"
    assert explanation["scope_filters"]["project_id"] == "project-1"

    pipeline.session_service.create_checkpoint(
        context_env["session"]["id"],
        agent_id="codex",
        completed=["schema", "API"],
        remaining=["tests"],
        constraints=["no push"],
        approved_decisions=["use one shared context service"],
    )
    refreshed = pipeline.build_context_v2(
        request(
            context_env,
            known_checkpoint_version=1,
            known_context_pack_id=first["context_pack_id"],
        )
    )
    assert refreshed["freshness"]["stale_reason"] == "source_changed"
    assert (
        refreshed["freshness"]["source_fingerprint"]
        != first["freshness"]["source_fingerprint"]
    )
    assert any(
        change["agent_id"] == "codex"
        for change in refreshed["session"]["delta"]["changes"]
    )


def test_feedback_requires_pack_scope_and_only_updates_target(context_env):
    service = context_env["pipeline"].context_service
    pack = context_env["pipeline"].build_context_v2(request(context_env))
    with pytest.raises(ContinuityError) as rejected:
        service.record_feedback(
            context_pack_id=pack["context_pack_id"],
            scope=scope(context_env, project_id="project-2"),
            agent_id="codex",
            used_memory_ids=[context_env["small_id"]],
        )
    assert rejected.value.code == "not_found"
    feedback = service.record_feedback(
        context_pack_id=pack["context_pack_id"],
        scope=scope(context_env),
        agent_id="codex",
        used_memory_ids=[context_env["small_id"]],
        idempotency_key="used-1",
    )
    assert feedback[0]["usage_type"] == "used"
    with sqlite3.connect(context_env["settings"].DB_PATH) as conn:
        unrelated = conn.execute(
            "SELECT last_used_at FROM memories WHERE id = ?",
            (context_env["other_id"],),
        ).fetchone()[0]
    assert unrelated is None


def test_candidate_only_does_not_extend_expiry(context_env):
    pipeline = context_env["pipeline"]
    before = pipeline.store.get(context_env["small_id"])
    assert before is not None
    pack = pipeline.build_context_v2(
        request(context_env, max_tokens=10, client_capabilities=("structured_json",))
    )
    after = pipeline.store.get(context_env["small_id"])
    assert after is not None
    assert context_env["small_id"] in pack["retrieval"]["omitted_memory_ids"]
    assert after["last_retrieved_at"] is not None
    assert after["last_selected_at"] is None
    assert after["expires_at"] == before["expires_at"]


def test_trust_provenance_and_score_components_survive_serialization(context_env):
    pack = context_env["pipeline"].build_context_v2(request(context_env))
    items = {item["id"]: item for item in pack["retrieval"]["results"]}
    repository = items[context_env["small_id"]]
    tool = items[context_env["long_id"]]
    assert repository["trust"]["authority"] == "repository_evidence"
    assert repository["provenance"]["source_ref"].endswith("context.py")
    assert repository["provenance"]["commit_sha"] == "abc123"
    assert tool["trust"]["authority"] == "successful_tool_output"
    assert repository["retrieval"]["keyword_score"] == pytest.approx(0.7)


def test_unverified_external_claim_is_referenced_for_validation(
    context_env, monkeypatch
):
    pipeline = context_env["pipeline"]
    task = context_env["task"]
    memory_id = pipeline.store.store(
        "Contradictory external claim about the active architecture.",
        "Contradictory external claim about the active architecture.",
        "external_claim",
        "active",
        [],
        source="https://example.invalid/claim",
        agent="external-agent",
        user_id=task["user_id"],
        workspace_id=task["workspace_id"],
        project_id=task["project_id"],
        repository_id=task["repository_id"],
        task_id=task["id"],
        session_id=context_env["session"]["id"],
    )
    memory = pipeline.store.get(memory_id)
    assert memory is not None
    monkeypatch.setattr(
        pipeline.hybrid_search,
        "search",
        lambda _query, **_kwargs: [{**memory, "score": 0.9, "sources": ["fts5"]}],
    )

    pack = pipeline.build_context_v2(request(context_env))

    result = pack["retrieval"]["results"][0]
    assert result["trust"]["authority"] == "external_content"
    assert result["trust"]["verification"] == "unverified"
    assert result["disposition"] == "reference"
    assert pack["validation_requests"][-1]["id"] == f"validate-{memory_id}"
    assert pack["validation_requests"][-1]["status"] == "pending"
    assert any(
        warning["code"] == "unresolved_validation" for warning in pack["warnings"]
    )


def test_persistence_survives_restart_and_admin_is_content_free(context_env, capsys):
    pack = context_env["pipeline"].build_context_v2(request(context_env))
    context_env["pipeline"].close()
    with MemoryPipeline(
        context_env["settings"], start_outbox_worker=False
    ) as restarted:
        loaded = restarted.context_service.get(
            pack["context_pack_id"], scope(context_env)
        )
    assert (
        loaded["freshness"]["source_fingerprint"]
        == pack["freshness"]["source_fingerprint"]
    )
    args = Namespace(action="inspect", context_pack_id=pack["context_pack_id"])
    assert command_context(context_env["settings"], args) == 0
    output = capsys.readouterr().out
    assert "context_pack_id" not in output
    assert "Successful test output" not in output
    explain_args = Namespace(action="explain", context_pack_id=pack["context_pack_id"])
    assert command_context(context_env["settings"], explain_args) == 0
    explanation = json.loads(capsys.readouterr().out)
    assert explanation["explanation"]["utility_affected_ranking"] is False
    missing_args = Namespace(action="inspect", context_pack_id="context-missing")
    assert command_context(context_env["settings"], missing_args) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "not_found"


def test_persistence_failure_does_not_mutate_task_session_or_memory(
    context_env, monkeypatch
):
    pipeline = context_env["pipeline"]
    before_task = pipeline.task_service.get_task(context_env["task"]["id"])
    before_checkpoint = pipeline.session_service.get_checkpoint(
        context_env["session"]["id"]
    )
    before_memory = pipeline.store.get(context_env["small_id"])

    def fail(**_kwargs):
        raise sqlite3.OperationalError("disk full captured-content")

    monkeypatch.setattr(pipeline.feedback_service, "persist_context_pack_v2", fail)
    with pytest.raises(sqlite3.OperationalError):
        pipeline.build_context_v2(request(context_env))
    after_memory = pipeline.store.get(context_env["small_id"])
    assert pipeline.task_service.get_task(context_env["task"]["id"]) == before_task
    assert (
        pipeline.session_service.get_checkpoint(context_env["session"]["id"])
        == before_checkpoint
    )
    assert after_memory["content"] == before_memory["content"]
    assert pipeline.context_service.health()["last_error"] == (
        "OperationalError: context build failed"
    )


def test_retrieval_telemetry_failure_fails_pack_without_corrupting_state(
    context_env, monkeypatch
):
    pipeline = context_env["pipeline"]

    def fail(**_kwargs):
        raise sqlite3.OperationalError("telemetry unavailable secret")

    monkeypatch.setattr(pipeline.feedback_service, "record_retrieval_run", fail)
    with pytest.raises(ContinuityError) as rejected:
        pipeline.build_context_v2(request(context_env))
    assert rejected.value.code == "telemetry_unavailable"
    with sqlite3.connect(context_env["settings"].DB_PATH) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM context_packs WHERE schema_version = 2"
            ).fetchone()[0]
            == 0
        )


def test_token_estimator_is_centralized_and_deterministic():
    estimator = TokenEstimator()
    assert estimator.estimate_text("abcd") == 1
    assert estimator.estimate_text("abcde") == 2
    assert estimator.estimate({"b": 2, "a": 1}) == estimator.estimate({"a": 1, "b": 2})
