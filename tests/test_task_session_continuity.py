from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from recall_mcp.continuity import (
    ContinuityError,
    ContinuityStore,
    SessionService,
    TaskService,
)
from recall_mcp.store.migrations import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    run_migrations,
)
from recall_mcp.store.store import MemoryStore
from recall_mcp.store.edges import EntityStore
from recall_mcp.search.hybrid import HybridSearch


@pytest.fixture
def services(tmp_path):
    store = ContinuityStore(tmp_path / "memory.db")
    tasks = TaskService(store)
    return store, tasks, SessionService(store, tasks)


def create_task(tasks: TaskService, **overrides):
    values = {
        "user_id": "user-1",
        "workspace_id": "workspace-1",
        "project_id": "project-1",
        "repository_id": "repo-1",
        "title": "Implement shared continuity",
        "objective": "Let agents exchange task checkpoints",
        "created_by": "claude-code",
        "agent_system_type": "claude_code",
        "canonical_path": "/work/recall",
        "default_branch": "feature/session",
    }
    values.update(overrides)
    return tasks.create_task(**values)


def test_current_migration_is_fresh_and_idempotent(tmp_path):
    db_path = tmp_path / "memory.db"
    first = run_migrations(db_path)
    repeated = run_migrations(db_path)
    assert first.to_version == CURRENT_SCHEMA_VERSION == 8
    assert repeated.from_version == repeated.to_version == 8
    assert repeated.applied == ()
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {
        "agents",
        "workspaces",
        "projects",
        "repositories",
        "tasks",
        "sessions",
        "session_participants",
        "session_events",
        "session_checkpoints",
    } <= tables


@pytest.mark.parametrize("baseline", range(1, 8))
def test_current_migration_upgrades_every_supported_baseline(tmp_path, baseline):
    db_path = tmp_path / f"baseline-{baseline}.db"
    run_migrations(db_path, MIGRATIONS[:baseline])
    upgraded = run_migrations(db_path)
    assert upgraded.from_version == baseline
    assert upgraded.to_version == 8
    assert [item.version for item in upgraded.applied] == list(range(baseline + 1, 9))


def test_task_create_read_update_complete_and_idempotency(services):
    _store, tasks, _sessions = services
    task = create_task(tasks, idempotency_key="create-1")
    replay = create_task(tasks, idempotency_key="create-1")
    assert replay["id"] == task["id"]
    assert replay["idempotent_replay"] is True
    assert tasks.get_task(task["id"], user_id="user-1") == {
        key: value for key, value in replay.items() if key != "idempotent_replay"
    }
    updated = tasks.update_task(task["id"], status="review", title="Review it")
    assert updated["status"] == "review"
    assert updated["title"] == "Review it"
    completed = tasks.complete_task(task["id"])
    assert completed["status"] == "completed"
    assert completed["completed_at"] is not None


def test_task_idempotency_conflict_and_scope_mismatch(services):
    _store, tasks, _sessions = services
    create_task(tasks, idempotency_key="same")
    with pytest.raises(ContinuityError, match="different task input"):
        create_task(tasks, idempotency_key="same", title="Different")
    with pytest.raises(ContinuityError, match="different scope"):
        create_task(
            tasks,
            user_id="user-2",
            workspace_id="workspace-2",
            project_id="project-2",
            repository_id="repo-1",
            canonical_path="/work/other",
        )


def test_task_scope_read_hides_cross_project(services):
    _store, tasks, _sessions = services
    task = create_task(tasks)
    with pytest.raises(ContinuityError) as error:
        tasks.get_task(task["id"], project_id="project-2")
    assert error.value.code == "not_found"


def test_formal_scope_is_filtered_before_hybrid_ranking(services):
    store, tasks, _sessions = services
    first_task = create_task(tasks)
    second_task = create_task(
        tasks,
        project_id="project-2",
        repository_id="repo-2",
        canonical_path="/work/other",
    )
    memories = MemoryStore(store.db_path)
    first_id = memories.store(
        "shared needle first",
        "first",
        "project",
        "active",
        [],
        user_id="user-1",
        workspace_id="workspace-1",
        project_id="project-1",
        repository_id="repo-1",
        task_id=first_task["id"],
    )
    memories.store(
        "shared needle second",
        "second",
        "project",
        "active",
        [],
        user_id="user-1",
        workspace_id="workspace-1",
        project_id="project-2",
        repository_id="repo-2",
        task_id=second_task["id"],
    )
    hybrid = HybridSearch(store.db_path, EntityStore(store.db_path))
    results = hybrid.search(
        "needle",
        mode="balanced",
        user_id="user-1",
        workspace_id="workspace-1",
        project_id="project-1",
        repository_id="repo-1",
        task_id=first_task["id"],
    )
    assert [result["id"] for result in results] == [first_id]


def test_session_start_join_and_duplicate_join(services):
    _store, tasks, sessions = services
    task = create_task(tasks)
    session = sessions.start_session(
        task_id=task["id"],
        agent_id="claude-code",
        idempotency_key="start-1",
    )
    replay = sessions.start_session(
        task_id=task["id"],
        agent_id="claude-code",
        idempotency_key="start-1",
    )
    assert replay["id"] == session["id"]
    joined = sessions.join_session(
        session["id"],
        agent_id="codex",
        role="implementer",
        agent_system_type="codex",
    )
    duplicate = sessions.join_session(session["id"], agent_id="codex", role="reviewer")
    assert len(joined["participants"]) == 2
    assert duplicate["idempotent_replay"] is True
    codex = next(p for p in duplicate["participants"] if p["agent_id"] == "codex")
    assert codex["role"] == "implementer"


def test_events_are_ordered_append_only_and_idempotent(services):
    store, tasks, sessions = services
    task = create_task(tasks)
    session = sessions.start_session(task_id=task["id"], agent_id="claude-code")
    event = sessions.append_event(
        session["id"],
        agent_id="claude-code",
        event_type="task.updated",
        payload={"constraints": ["preserve compatibility"]},
        idempotency_key="event-1",
    )
    replay = sessions.append_event(
        session["id"],
        agent_id="claude-code",
        event_type="task.updated",
        payload={"constraints": ["preserve compatibility"]},
        idempotency_key="event-1",
    )
    assert replay["id"] == event["id"]
    assert replay["idempotent_replay"] is True
    with pytest.raises(ContinuityError) as error:
        sessions.append_event(
            session["id"],
            agent_id="claude-code",
            event_type="work.completed",
            idempotency_key="event-1",
        )
    assert error.value.code == "idempotency_conflict"
    with sqlite3.connect(store.db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE session_events SET payload_json = '{}' WHERE id = ?",
                (event["id"],),
            )


def test_checkpoint_versions_and_delta_semantics(services):
    _store, tasks, sessions = services
    task = create_task(tasks)
    session = sessions.start_session(task_id=task["id"], agent_id="claude-code")
    first = sessions.create_checkpoint(
        session["id"],
        agent_id="claude-code",
        summary="Phase 0 complete",
        completed=["outbox"],
        remaining=["sessions"],
        constraints=["no API regression"],
        approved_decisions=["SQLite"],
        open_questions=["NATS notifications?"],
        important_files=["src/recall_mcp/pipeline.py"],
        idempotency_key="checkpoint-1",
    )
    assert first["version"] == 1
    replay = sessions.create_checkpoint(
        session["id"],
        agent_id="claude-code",
        summary="Phase 0 complete",
        completed=["outbox"],
        remaining=["sessions"],
        constraints=["no API regression"],
        approved_decisions=["SQLite"],
        open_questions=["NATS notifications?"],
        important_files=["src/recall_mcp/pipeline.py"],
        idempotency_key="checkpoint-1",
    )
    assert replay["version"] == 1
    sessions.join_session(session["id"], agent_id="codex")
    sessions.append_event(
        session["id"],
        agent_id="codex",
        event_type="work.completed",
        payload={"work": "session service", "tests": "passed"},
    )
    second = sessions.create_checkpoint(
        session["id"],
        agent_id="codex",
        summary="Session service complete",
        completed=["outbox", "session service"],
        remaining=["REST"],
        constraints=["no API regression"],
        approved_decisions=["SQLite"],
        blockers=["none"],
    )
    assert second["version"] == 2
    delta = sessions.get_delta(session["id"], known_version=1, agent_id="claude-code")
    assert delta["from_version"] == 1
    assert delta["to_version"] == 2
    assert delta["completed_added"] == ["session service"]
    assert delta["remaining_changed"] == ["REST"]
    assert [event["sequence"] for event in delta["events"]] == sorted(
        event["sequence"] for event in delta["events"]
    )
    current = sessions.get_delta(session["id"], known_version=2, agent_id="claude-code")
    assert current["no_change"] is True
    assert current["checkpoint"] is None
    with pytest.raises(ContinuityError) as error:
        sessions.get_delta(session["id"], known_version=3)
    assert error.value.code == "invalid_version"


def test_concurrent_events_have_unique_deterministic_order(services):
    _store, tasks, sessions = services
    task = create_task(tasks)
    session = sessions.start_session(task_id=task["id"], agent_id="claude-code")

    def append(index: int):
        return sessions.append_event(
            session["id"],
            agent_id="claude-code",
            event_type="task.updated",
            payload={"index": index},
            idempotency_key=f"concurrent-{index}",
        )

    with ThreadPoolExecutor(max_workers=6) as executor:
        events = list(executor.map(append, range(12)))
    assert len({event["sequence"] for event in events}) == 12
    delta = sessions.get_delta(session["id"], known_version=0)
    sequences = [event["sequence"] for event in delta["events"]]
    assert sequences == sorted(sequences)


def test_close_keeps_history_readable_and_blocks_writes(services):
    _store, tasks, sessions = services
    task = create_task(tasks)
    session = sessions.start_session(task_id=task["id"], agent_id="claude-code")
    sessions.create_checkpoint(session["id"], agent_id="claude-code")
    closed = sessions.close_session(
        session["id"], agent_id="claude-code", idempotency_key="close-1"
    )
    assert closed["status"] == "closed"
    assert sessions.get_checkpoint(session["id"], 1)["version"] == 1
    assert sessions.get_delta(session["id"], known_version=0)["events"]
    with pytest.raises(ContinuityError, match="read-only"):
        sessions.append_event(
            session["id"],
            agent_id="claude-code",
            event_type="task.updated",
        )


def test_cross_agent_handoff_needs_no_transcript_or_restatement(services):
    _store, tasks, sessions = services
    task = create_task(tasks, title="Handoff", objective="Ship Phase 1")
    session = sessions.start_session(
        task_id=task["id"], agent_id="claude-code", idempotency_key="start"
    )
    sessions.append_event(
        session["id"],
        agent_id="claude-code",
        event_type="task.updated",
        payload={"objective": task["objective"], "constraint": "no push"},
        idempotency_key="objective",
    )
    sessions.create_checkpoint(
        session["id"],
        agent_id="claude-code",
        summary="Claude checkpoint",
        completed=["schema"],
        remaining=["service", "API"],
        constraints=["no push"],
        approved_decisions=["structured checkpoint"],
        idempotency_key="cp-1",
    )
    sessions.join_session(session["id"], agent_id="codex", idempotency_key="join-codex")
    codex_delta = sessions.get_delta(session["id"], known_version=0, agent_id="codex")
    assert codex_delta["checkpoint"]["constraints"] == ["no push"]
    assert (
        codex_delta["checkpoint"]["objective"]
        if "objective" in codex_delta["checkpoint"]
        else task["objective"]
    )
    sessions.append_event(
        session["id"],
        agent_id="codex",
        event_type="work.completed",
        payload={"completed": "service", "validation": "tests pass"},
        idempotency_key="codex-work",
    )
    sessions.create_checkpoint(
        session["id"],
        agent_id="codex",
        summary="Codex checkpoint",
        completed=["schema", "service"],
        remaining=["API"],
        constraints=["no push"],
        approved_decisions=["structured checkpoint"],
        idempotency_key="cp-2",
    )
    claude_delta = sessions.get_delta(
        session["id"], known_version=1, agent_id="claude-code"
    )
    assert claude_delta["completed_added"] == ["service"]
    assert all(event["agent_id"] in {"codex"} for event in claude_delta["events"])
    replay = sessions.append_event(
        session["id"],
        agent_id="codex",
        event_type="work.completed",
        payload={"completed": "service", "validation": "tests pass"},
        idempotency_key="codex-work",
    )
    assert replay["idempotent_replay"] is True
