from __future__ import annotations

import json
import sqlite3
import time
from argparse import Namespace

import pytest

from recall_mcp.admin import command_utility
from recall_mcp.continuity import (
    ContinuityError,
    ContinuityStore,
    SessionService,
    TaskService,
)
from recall_mcp.feedback import RetrievalFeedbackService
from recall_mcp.runtime import Settings
from recall_mcp.search.hybrid import HybridSearch
from recall_mcp.store.edges import EntityStore
from recall_mcp.store.store import MemoryStore


@pytest.fixture
def utility_env(tmp_path):
    settings = Settings(
        BASE_DIR=tmp_path,
        DB_PATH=tmp_path / "memory.db",
        EMBEDDINGS_ENABLED=False,
    )
    continuity = ContinuityStore(settings.DB_PATH)
    tasks = TaskService(continuity)
    sessions = SessionService(continuity, tasks)
    task = tasks.create_task(
        user_id="user-1",
        workspace_id="workspace-1",
        project_id="project-1",
        repository_id="repo-1",
        title="Measure retrieval utility",
        objective="Learn which context helps",
        created_by="claude-code",
        canonical_path="/work/recall",
    )
    session = sessions.start_session(task_id=task["id"], agent_id="claude-code")
    memory_store = MemoryStore(settings.DB_PATH)
    feedback = RetrievalFeedbackService(settings.DB_PATH, settings)
    return {
        "settings": settings,
        "tasks": tasks,
        "sessions": sessions,
        "task": task,
        "session": session,
        "store": memory_store,
        "feedback": feedback,
    }


def make_memory(env, text="useful constraint", **overrides):
    task = env["task"]
    values = {
        "content": text,
        "summary": text,
        "category": "project",
        "tier": "active",
        "key_topics": [],
        "agent": "claude-code",
        "user_id": task["user_id"],
        "workspace_id": task["workspace_id"],
        "project_id": task["project_id"],
        "repository_id": task["repository_id"],
        "task_id": task["id"],
        "session_id": env["session"]["id"],
    }
    values.update(overrides)
    return env["store"].store(**values)


def make_run(env, memory_ids, *, session_id=None, agent_id="claude-code", key=None):
    task = env["task"]
    results = [
        {
            "id": memory_id,
            "score": 0.9 - rank * 0.1,
            "keyword_score": 0.8 - rank * 0.1,
            "sources": ["fts5"],
        }
        for rank, memory_id in enumerate(memory_ids)
    ]
    run_id = env["feedback"].record_retrieval_run(
        query="constraint",
        mode="balanced",
        requested_limit=10,
        latency_ms=2.5,
        results=results,
        user_id=task["user_id"],
        workspace_id=task["workspace_id"],
        project_id=task["project_id"],
        repository_id=task["repository_id"],
        task_id=task["id"],
        session_id=session_id or env["session"]["id"],
        agent_id=agent_id,
        idempotency_key=key,
    )
    return run_id, results


def make_pack(env, memory_ids, **kwargs):
    run_id, results = make_run(env, memory_ids, **kwargs)
    pack_id = env["feedback"].create_context_pack(
        run_id,
        task_id=env["task"]["id"],
        session_id=kwargs.get("session_id") or env["session"]["id"],
        agent_id=kwargs.get("agent_id", "claude-code"),
        token_budget=1000,
        estimated_tokens=25,
    )
    return run_id, pack_id, results


def memory_row(env, memory_id):
    with sqlite3.connect(env["settings"].DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return dict(
            conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        )


def test_retrieval_run_is_idempotent_and_preserves_real_score_components(utility_env):
    memory_id = make_memory(utility_env)
    before_expiry = memory_row(utility_env, memory_id)["expires_at"]
    run_id, results = make_run(utility_env, [memory_id], key="run-1")
    replay_id, _ = make_run(utility_env, [memory_id], key="run-1")
    assert replay_id == run_id
    assert results[0]["retrieval_run_id"] == run_id
    assert results[0]["production_rank"] == 1
    with sqlite3.connect(utility_env["settings"].DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        assert conn.execute("SELECT COUNT(*) FROM retrieval_runs").fetchone()[0] == 1
        result = conn.execute("SELECT * FROM retrieval_results").fetchone()
        assert result["keyword_score"] == pytest.approx(0.8)
        assert result["vector_score"] is None
        assert result["graph_score"] is None
    after = memory_row(utility_env, memory_id)
    assert after["last_retrieved_at"] is not None
    assert after["expires_at"] == before_expiry


def test_retrieval_idempotency_rejects_conflicts_and_scope_mismatch(utility_env):
    first = make_memory(utility_env, text="first")
    second = make_memory(utility_env, text="second")
    make_run(utility_env, [first], key="same-run")
    with pytest.raises(ContinuityError) as conflict:
        make_run(utility_env, [second], key="same-run")
    assert conflict.value.code == "idempotency_conflict"

    task = utility_env["task"]
    with pytest.raises(ContinuityError) as scope_error:
        utility_env["feedback"].record_retrieval_run(
            query="constraint",
            mode="balanced",
            requested_limit=10,
            latency_ms=1,
            results=[{"id": first, "score": 1.0}],
            user_id=task["user_id"],
            workspace_id=task["workspace_id"],
            project_id="another-project",
            repository_id=task["repository_id"],
            task_id=task["id"],
            session_id=utility_env["session"]["id"],
            agent_id="claude-code",
        )
    assert scope_error.value.code == "scope_mismatch"


def test_context_pack_cannot_change_retrieval_scope(utility_env):
    memory_id = make_memory(utility_env)
    run_id, _results = make_run(utility_env, [memory_id])
    with pytest.raises(ContinuityError) as error:
        utility_env["feedback"].create_context_pack(
            run_id,
            task_id=utility_env["task"]["id"],
            session_id=utility_env["session"]["id"],
            agent_id="codex",
            token_budget=100,
            estimated_tokens=10,
        )
    assert error.value.code == "scope_mismatch"


def test_context_selection_and_injection_are_distinct(utility_env):
    memory_id = make_memory(utility_env)
    initial_score, _ = utility_env["feedback"].calculate_utility(memory_id)
    with sqlite3.connect(utility_env["settings"].DB_PATH) as conn:
        conn.execute(
            "UPDATE memories SET expires_at = ? WHERE id = ?",
            (time.time() + 10, memory_id),
        )
    run_id, pack_id, _results = make_pack(utility_env, [memory_id])
    selected = memory_row(utility_env, memory_id)
    selected_score, _ = utility_env["feedback"].calculate_utility(memory_id)
    assert selected["last_selected_at"] is not None
    assert selected["last_injected_at"] is None
    assert selected["expires_at"] > time.time() + 80000
    assert selected_score > initial_score
    utility_env["feedback"].mark_context_injected(pack_id, agent_id="claude-code")
    injected = memory_row(utility_env, memory_id)
    injected_score, _ = utility_env["feedback"].calculate_utility(memory_id)
    assert injected["last_injected_at"] is not None
    assert injected_score > selected_score
    with sqlite3.connect(utility_env["settings"].DB_PATH) as conn:
        flags = conn.execute(
            "SELECT selected, injected, used FROM retrieval_results WHERE retrieval_run_id = ?",
            (run_id,),
        ).fetchone()
    assert flags == (1, 1, 0)


def test_feedback_is_idempotent_and_usage_states_remain_distinct(utility_env):
    memory_id = make_memory(utility_env)
    _run, pack_id, _results = make_pack(utility_env, [memory_id])
    first = utility_env["feedback"].record_context_usage(
        context_pack_id=pack_id,
        memory_id=memory_id,
        agent_id="codex",
        usage_type="expanded",
        idempotency_key="expand-1",
    )
    replay = utility_env["feedback"].record_context_usage(
        context_pack_id=pack_id,
        memory_id=memory_id,
        agent_id="codex",
        usage_type="expanded",
        idempotency_key="expand-1",
    )
    assert replay["id"] == first["id"]
    assert replay["idempotent_replay"] is True
    utility_env["feedback"].record_context_usage(
        context_pack_id=pack_id,
        memory_id=memory_id,
        agent_id="codex",
        usage_type="used",
        idempotency_key="used-1",
    )
    with sqlite3.connect(utility_env["settings"].DB_PATH) as conn:
        types = {
            row[0]
            for row in conn.execute(
                "SELECT usage_type FROM context_usage WHERE memory_id = ?",
                (memory_id,),
            )
        }
    assert {"returned", "selected", "expanded", "used"} <= types


def test_utility_is_deterministic_and_positive_reuse_increases_score(utility_env):
    memory_id = make_memory(utility_env)
    now = time.time()
    initial, components = utility_env["feedback"].calculate_utility(memory_id, now=now)
    repeat, repeated_components = utility_env["feedback"].calculate_utility(
        memory_id, now=now
    )
    assert repeat == initial
    assert repeated_components == components
    _run, pack_id, _results = make_pack(utility_env, [memory_id])
    utility_env["feedback"].record_context_usage(
        context_pack_id=pack_id,
        memory_id=memory_id,
        agent_id="claude-code",
        usage_type="used",
    )
    reused, _ = utility_env["feedback"].calculate_utility(memory_id, now=now)
    assert 0 <= initial < reused <= 1


def test_cross_session_and_cross_agent_reuse_increase_utility(utility_env):
    memory_id = make_memory(utility_env)
    _run, first_pack, _results = make_pack(utility_env, [memory_id])
    utility_env["feedback"].record_context_usage(
        context_pack_id=first_pack,
        memory_id=memory_id,
        agent_id="claude-code",
        usage_type="used",
    )
    one_score, _ = utility_env["feedback"].calculate_utility(memory_id)
    second_session = utility_env["sessions"].start_session(
        task_id=utility_env["task"]["id"], agent_id="codex"
    )
    _run, second_pack, _results = make_pack(
        utility_env,
        [memory_id],
        session_id=second_session["id"],
        agent_id="codex",
    )
    utility_env["feedback"].record_context_usage(
        context_pack_id=second_pack,
        memory_id=memory_id,
        agent_id="codex",
        usage_type="used",
    )
    cross_score, components = utility_env["feedback"].calculate_utility(memory_id)
    assert cross_score > one_score
    assert components.cross_session_reuse > 0
    assert components.cross_agent_reuse > 0


def test_user_correction_outweighs_agent_usefulness(utility_env):
    memory_id = make_memory(utility_env)
    _run, pack_id, _results = make_pack(utility_env, [memory_id])
    utility_env["feedback"].record_context_usage(
        context_pack_id=pack_id,
        memory_id=memory_id,
        agent_id="codex",
        usage_type="used",
    )
    useful, _ = utility_env["feedback"].calculate_utility(memory_id)
    utility_env["feedback"].record_context_usage(
        context_pack_id=pack_id,
        memory_id=memory_id,
        agent_id="codex",
        usage_type="corrected",
    )
    corrected, components = utility_env["feedback"].calculate_utility(memory_id)
    assert corrected < useful
    assert components.correction_penalty > components.successful_reuse


def test_repeated_ignores_and_contradiction_reduce_utility(utility_env):
    memory_id = make_memory(utility_env)
    _run, pack_id, _results = make_pack(utility_env, [memory_id])
    before, _ = utility_env["feedback"].calculate_utility(memory_id)
    for index in range(2):
        utility_env["feedback"].record_context_usage(
            context_pack_id=pack_id,
            memory_id=memory_id,
            agent_id="codex",
            usage_type="ignored",
            metadata={"contradiction": index == 1},
            idempotency_key=f"ignored-{index}",
        )
    after, components = utility_env["feedback"].calculate_utility(memory_id)
    assert after < before
    assert components.ignored_penalty > 0
    assert components.contradiction_penalty > 0


def test_rejection_is_a_stronger_negative_than_one_ignore(utility_env):
    ignored_memory = make_memory(utility_env, text="ignored")
    rejected_memory = make_memory(utility_env, text="rejected")
    _run, pack_id, _results = make_pack(utility_env, [ignored_memory, rejected_memory])
    utility_env["feedback"].record_context_usage(
        context_pack_id=pack_id,
        memory_id=ignored_memory,
        agent_id="codex",
        usage_type="ignored",
    )
    utility_env["feedback"].record_context_usage(
        context_pack_id=pack_id,
        memory_id=rejected_memory,
        agent_id="codex",
        usage_type="rejected",
    )
    ignored_score, ignored_components = utility_env["feedback"].calculate_utility(
        ignored_memory
    )
    rejected_score, rejected_components = utility_env["feedback"].calculate_utility(
        rejected_memory
    )
    assert rejected_score < ignored_score
    assert rejected_components.ignored_penalty > ignored_components.ignored_penalty


def test_canonical_update_refreshes_expiry_and_retention_review(utility_env):
    memory_id = make_memory(utility_env)
    with sqlite3.connect(utility_env["settings"].DB_PATH) as conn:
        conn.execute(
            """
            UPDATE memories SET expires_at = ?, retention_review_at = NULL
            WHERE id = ?
            """,
            (time.time() + 1, memory_id),
        )
    utility_env["store"].update_enrichment(
        memory_id,
        summary="trusted update",
        category="project",
        tier="active",
        key_topics=["updated"],
    )
    updated = memory_row(utility_env, memory_id)
    assert updated["expires_at"] > time.time() + 29 * 86400
    assert updated["retention_review_at"] > time.time() + 29 * 86400


def test_task_outcome_records_success_and_extends_expiry(utility_env):
    memory_id = make_memory(utility_env)
    _run, pack_id, _results = make_pack(utility_env, [memory_id])
    utility_env["feedback"].record_context_usage(
        context_pack_id=pack_id,
        memory_id=memory_id,
        agent_id="codex",
        usage_type="used",
    )
    before = memory_row(utility_env, memory_id)["expires_at"]
    result = utility_env["feedback"].record_task_outcome(
        task_id=utility_env["task"]["id"],
        session_id=utility_env["session"]["id"],
        status="completed",
        successful=True,
        agent_id="codex",
        idempotency_key="outcome-1",
    )
    replay = utility_env["feedback"].record_task_outcome(
        task_id=utility_env["task"]["id"],
        session_id=utility_env["session"]["id"],
        status="completed",
        successful=True,
        agent_id="codex",
        idempotency_key="outcome-1",
    )
    assert result["successful"] is True
    assert replay["id"] == result["id"]
    after = memory_row(utility_env, memory_id)
    assert after["last_successful_use_at"] is not None
    assert after["expires_at"] > before
    with sqlite3.connect(utility_env["settings"].DB_PATH) as conn:
        assert (
            conn.execute("SELECT agent_id FROM task_outcomes").fetchone()[0] == "codex"
        )


def test_failed_rework_outcome_has_no_success_bonus_and_adds_penalty(utility_env):
    memory_id = make_memory(utility_env)
    _run, pack_id, _results = make_pack(utility_env, [memory_id])
    utility_env["feedback"].record_context_usage(
        context_pack_id=pack_id,
        memory_id=memory_id,
        agent_id="codex",
        usage_type="used",
    )
    utility_env["feedback"].record_task_outcome(
        task_id=utility_env["task"]["id"],
        session_id=utility_env["session"]["id"],
        status="failed",
        successful=False,
        agent_id="codex",
        rework_required=True,
        idempotency_key="failed-outcome",
    )
    _score, components = utility_env["feedback"].calculate_utility(memory_id)
    assert components.task_success == 0
    assert components.contradiction_penalty > 0


def test_pin_protects_expiry_and_unpin_is_audited(utility_env):
    memory_id = make_memory(utility_env)
    pinned = utility_env["feedback"].pin(memory_id, actor_id="user-agent")
    assert pinned["lifecycle_state"] == "pinned"
    assert memory_row(utility_env, memory_id)["expires_at"] is None
    utility_env["feedback"].review_expired(now=time.time() + 10 * 365 * 86400)
    assert memory_row(utility_env, memory_id)["lifecycle_state"] == "pinned"
    restored = utility_env["feedback"].unpin(memory_id, actor_id="user-agent")
    assert restored["lifecycle_state"] in {"active", "stable"}
    with sqlite3.connect(utility_env["settings"].DB_PATH) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM memory_lifecycle_audit WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()[0]
            == 2
        )


def test_cold_demotion_is_reversible_readable_and_excluded_normally(utility_env):
    memory_id = make_memory(utility_env, text="cold marker")
    hybrid = HybridSearch(
        utility_env["settings"].DB_PATH,
        EntityStore(utility_env["settings"].DB_PATH),
    )
    assert hybrid.search("cold marker", mode="keyword")
    utility_env["feedback"].demote_to_cold(memory_id, reason="manual")
    assert utility_env["store"].get(memory_id)["id"] == memory_id
    assert hybrid.search("cold marker", mode="keyword") == []
    explicit = hybrid.search("cold marker", mode="keyword", include_cold=True)
    assert [result["id"] for result in explicit] == [memory_id]
    utility_env["feedback"].restore_from_cold(memory_id)
    assert hybrid.search("cold marker", mode="keyword")


def test_restore_refreshes_an_expired_cold_memory(utility_env):
    memory_id = make_memory(utility_env, text="restore expired marker")
    utility_env["feedback"].demote_to_cold(memory_id, reason="test")
    with sqlite3.connect(utility_env["settings"].DB_PATH) as conn:
        conn.execute(
            "UPDATE memories SET expires_at = ? WHERE id = ?",
            (time.time() - 1, memory_id),
        )
    utility_env["feedback"].restore_from_cold(memory_id)
    restored = memory_row(utility_env, memory_id)
    assert restored["expires_at"] > time.time()
    hybrid = HybridSearch(
        utility_env["settings"].DB_PATH,
        EntityStore(utility_env["settings"].DB_PATH),
    )
    assert hybrid.search("restore expired marker", mode="keyword")


def test_feedback_cannot_target_memory_outside_context_pack(utility_env):
    first = make_memory(utility_env, text="first")
    other = make_memory(utility_env, text="other")
    _run, pack_id, _results = make_pack(utility_env, [first])
    with pytest.raises(ContinuityError) as error:
        utility_env["feedback"].record_context_usage(
            context_pack_id=pack_id,
            memory_id=other,
            agent_id="codex",
            usage_type="used",
        )
    assert error.value.code == "not_found"


def test_shadow_report_does_not_change_production_order(utility_env):
    first = make_memory(utility_env, text="first")
    second = make_memory(utility_env, text="second")
    with sqlite3.connect(utility_env["settings"].DB_PATH) as conn:
        conn.execute("UPDATE memories SET utility_score = 0 WHERE id = ?", (first,))
        conn.execute("UPDATE memories SET utility_score = 1 WHERE id = ?", (second,))
    _run, results = make_run(utility_env, [first, second])
    assert [result["id"] for result in results] == [first, second]
    assert results[0]["production_rank"] == 1
    assert results[1]["shadow_rank"] == 1
    report = utility_env["feedback"].shadow_report()
    assert report["production_ranking_changed"] is False
    assert report["shadow_mode"] is True


def test_configurable_weights_policy_version_and_clamping(tmp_path):
    settings = Settings(
        BASE_DIR=tmp_path,
        DB_PATH=tmp_path / "memory.db",
        EMBEDDINGS_ENABLED=False,
        UTILITY_POLICY_VERSION="utility-test-v2",
        UTILITY_WEIGHTS={"pinned": 100.0},
    )
    store = MemoryStore(settings.DB_PATH)
    memory_id = store.store("pin me", "pin", "project", "active", [])
    feedback = RetrievalFeedbackService(settings.DB_PATH, settings)
    feedback.pin(memory_id)
    explanation = feedback.explain_utility(memory_id)
    assert explanation["calculated_score"] == 1.0
    assert explanation["policy_version"] == "utility-test-v2"


def test_consolidation_demotes_without_permanent_deletion(utility_env):
    memory_id = make_memory(utility_env, tier="cold")
    with sqlite3.connect(utility_env["settings"].DB_PATH) as conn:
        conn.execute(
            "UPDATE memories SET expires_at = ? WHERE id = ?",
            (time.time() - 1, memory_id),
        )
    result = utility_env["store"].consolidate()
    assert result["expired_deleted"] == 0
    assert result["expired_demoted"] == 1
    assert utility_env["store"].get(memory_id)["lifecycle_state"] == "cold"
    with sqlite3.connect(utility_env["settings"].DB_PATH) as conn:
        audit = conn.execute(
            """
            SELECT previous_state, new_state, reason
            FROM memory_lifecycle_audit WHERE memory_id = ?
            """,
            (memory_id,),
        ).fetchone()
    assert audit == ("ephemeral", "cold", "retention_review")


def test_admin_utility_status_and_recovery_do_not_start_worker(utility_env, capsys):
    memory_id = make_memory(utility_env)
    settings = utility_env["settings"]
    assert (
        command_utility(
            settings,
            Namespace(action="shadow-report", memory_id=None, limit=10, reason=None),
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["policy_version"] == "utility-v1"
    assert report["production_ranking_changed"] is False

    for action, expected in (("cold", "cold"), ("restore", "active")):
        assert (
            command_utility(
                settings,
                Namespace(
                    action=action,
                    memory_id=memory_id,
                    limit=10,
                    reason="test",
                ),
            )
            == 0
        )
        result = json.loads(capsys.readouterr().out)
        assert result["lifecycle_state"] == expected
