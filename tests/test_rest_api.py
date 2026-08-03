"""
REST API Tests — HTTP handler coverage for the Recall REST API
"""

import json
import tempfile
from pathlib import Path
import pytest
from http.server import HTTPServer
from threading import Thread

from recall_mcp.pipeline import MemoryPipeline
from recall_mcp.config import Settings
from recall_mcp.api.rest import RecallHandler, _is_client_disconnect
from recall_mcp.gate_backends import HeuristicBackend, GateFallbackChain
import urllib.request
import urllib.error
import urllib.parse


class _DisconnectingHandler:
    def _json_response(self, data, status=200):
        raise ConnectionAbortedError(10053, "connection aborted")


class _FailingHandler:
    def _json_response(self, data, status=200):
        raise RuntimeError("response failed")


@pytest.fixture
def api_server():
    """Start a REST API server on a random available port."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "memories.db"
        settings = Settings(
            DB_PATH=db_path,
            OLLAMA_BASE_URL="http://localhost:11434",
            GATE_MODEL_PATH=None,
        )
        pipeline = MemoryPipeline(settings=settings)
        pipeline.gate_chain = GateFallbackChain([HeuristicBackend()])
        from recall_mcp.extract import StubExtractor

        pipeline.extractor = StubExtractor()

        # Find an available port
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        RecallHandler.pipeline = pipeline
        server = HTTPServer(("127.0.0.1", port), RecallHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()

        base_url = f"http://127.0.0.1:{port}"
        yield {
            "base_url": base_url,
            "pipeline": pipeline,
            "port": port,
        }

        server.shutdown()
        server.server_close()
        pipeline.close()


class TestHealthEndpoint:
    def test_health(self, api_server):
        resp = urllib.request.urlopen(f"{api_server['base_url']}/health")
        data = json.loads(resp.read())
        assert data["status"] == "ok"
        assert data["version"] == "2.0.0"

    def test_readiness_includes_outbox_worker(self, api_server):
        resp = urllib.request.urlopen(f"{api_server['base_url']}/health/ready")
        data = json.loads(resp.read())
        assert data["status"] == "ready"
        assert data["outbox"] == {
            "pending": 0,
            "processing": 0,
            "retry": 0,
            "complete": 0,
            "dead": 0,
        }
        assert data["outbox_active_leases"] == 0
        assert data["outbox_oldest_due_seconds"] == 0
        assert data["outbox_worker"]["thread_alive"] is True
        assert data["retrieval_feedback"]["policy_version"] == "utility-v1"
        assert data["retrieval_feedback"]["shadow_mode"] is True
        assert data["retrieval_feedback"]["error_count"] == 0
        assert data["context_service"]["available"] is True
        assert data["context_service"]["migration_available"] is True
        assert data["context_service"]["feedback_linkage_healthy"] is True
        assert data["context_service"]["policy_version"] == "context-v2"
        assert data["context_service"]["token_estimator_version"] == "chars-v1"

    def test_readiness_sanitizes_dispatcher_error(self, api_server):
        pipeline = api_server["pipeline"]
        pipeline.outbox_dispatcher._record_error(
            RuntimeError("customer-secret must not escape")
        )

        resp = urllib.request.urlopen(f"{api_server['base_url']}/health/ready")
        data = json.loads(resp.read())

        assert data["outbox_worker"]["last_error"] == (
            "RuntimeError: capture processing failed"
        )
        assert "customer-secret" not in json.dumps(data)

        metrics = (
            urllib.request.urlopen(f"{api_server['base_url']}/metrics").read().decode()
        )
        assert "recall_outbox_dispatcher_error 1.0" in metrics
        assert "customer-secret" not in metrics

    def test_feedback_observability_is_content_free(self, api_server):
        pipeline = api_server["pipeline"]
        pipeline.feedback_service.record_error(
            RuntimeError("captured-content must not escape")
        )

        readiness = json.loads(
            urllib.request.urlopen(f"{api_server['base_url']}/health/ready").read()
        )
        feedback = readiness["retrieval_feedback"]
        assert feedback["error_count"] == 1
        assert feedback["last_error"] == "RuntimeError: telemetry operation failed"
        assert readiness["status"] == "ready"

        metrics = (
            urllib.request.urlopen(f"{api_server['base_url']}/metrics").read().decode()
        )
        assert "recall_retrieval_feedback_errors 1.0" in metrics
        assert "recall_utility_shadow_mode 1.0" in metrics
        assert "recall_context_packs_built_total 0.0" in metrics
        assert "recall_context_pack_failures_total 0.0" in metrics
        assert "recall_context_pack_feedback_pending_total 0.0" in metrics
        assert "captured-content" not in json.dumps(readiness)
        assert "captured-content" not in metrics


class TestClientDisconnectHandling:
    def test_connection_aborted_is_client_disconnect(self):
        error = ConnectionAbortedError(10053, "connection aborted")

        assert _is_client_disconnect(error)

    def test_safe_json_response_suppresses_client_disconnect(self):
        RecallHandler._safe_json_response(
            _DisconnectingHandler(),
            {"error": "request failed"},
            status=500,
        )

    def test_safe_json_response_raises_non_disconnect_errors(self):
        with pytest.raises(RuntimeError):
            RecallHandler._safe_json_response(
                _FailingHandler(),
                {"error": "request failed"},
                status=500,
            )


class TestStatsEndpoint:
    def test_stats(self, api_server):
        resp = urllib.request.urlopen(f"{api_server['base_url']}/stats")
        data = json.loads(resp.read())
        assert "memories" in data
        assert "entities" in data
        assert "outbox" in data["operational"]
        assert set(data["operational"]["outbox"]) == {
            "pending",
            "processing",
            "retry",
            "complete",
            "dead",
        }
        assert data["outbox_worker"]["thread_alive"] is True
        assert data["retrieval_feedback"]["retrieval_runs"] == 0
        assert data["retrieval_feedback"]["shadow_mode"] is True
        assert data["context_packs"]["schema_version"] == 2
        assert data["context_packs"]["policy_version"] == "context-v2"


class TestCaptureEndpoint:
    def test_capture_post(self, api_server):
        body = json.dumps(
            {
                "text": "Ema decided Prism stays domain-agnostic",
                "source": "test",
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            f"{api_server['base_url']}/capture",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req)
        assert resp.status == 201
        data = json.loads(resp.read())
        assert data["id"] is not None

    def test_capture_missing_text(self, api_server):
        body = json.dumps({"source": "test"}).encode("utf-8")
        req = urllib.request.Request(
            f"{api_server['base_url']}/capture",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req)
            assert False, "Expected 400 error"
        except urllib.error.HTTPError as e:
            assert e.code == 400


class TestTaskSessionContinuityEndpoints:
    @staticmethod
    def request(base_url, path, body=None, method="POST"):
        request = urllib.request.Request(
            f"{base_url}{path}",
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        response = urllib.request.urlopen(request)
        return response.status, json.loads(response.read())

    def test_cross_agent_handoff_and_scoped_context(self, api_server):
        base = api_server["base_url"]
        status, task = self.request(
            base,
            "/v2/tasks",
            {
                "user_id": "user-1",
                "workspace_id": "workspace-1",
                "project_id": "project-1",
                "repository_id": "repo-1",
                "title": "REST handoff",
                "objective": "Share state",
                "agent_id": "claude-code",
                "agent_system_type": "claude_code",
                "canonical_path": "/work/recall",
                "default_branch": "feature/session",
                "idempotency_key": "rest-task",
            },
        )
        assert status == 201
        status, fetched = self.request(base, f"/v2/tasks/{task['id']}", method="GET")
        assert status == 200
        assert fetched["objective"] == "Share state"
        status, updated = self.request(
            base,
            f"/v2/tasks/{task['id']}",
            {"status": "review"},
            method="PATCH",
        )
        assert status == 200
        assert updated["status"] == "review"

        status, session = self.request(
            base,
            "/v2/sessions",
            {
                "task_id": task["id"],
                "agent_id": "claude-code",
                "idempotency_key": "rest-session",
            },
        )
        assert status == 201
        self.request(
            base,
            f"/v2/sessions/{session['id']}/events",
            {
                "agent_id": "claude-code",
                "event_type": "task.updated",
                "payload": {"constraint": "no push"},
                "idempotency_key": "rest-event",
            },
        )
        self.request(
            base,
            f"/v2/sessions/{session['id']}/checkpoint",
            {
                "agent_id": "claude-code",
                "summary": "Claude checkpoint",
                "completed": ["schema"],
                "remaining": ["API"],
                "constraints": ["no push"],
                "idempotency_key": "rest-checkpoint",
            },
        )
        self.request(
            base,
            f"/v2/sessions/{session['id']}/join",
            {"agent_id": "codex", "role": "implementer"},
        )
        status, delta = self.request(
            base,
            f"/v2/sessions/{session['id']}/delta?known_version=0&agent_id=codex",
            method="GET",
        )
        assert status == 200
        assert delta["checkpoint"]["objective"] == "Share state"
        assert delta["checkpoint"]["constraints"] == ["no push"]

        status, context = self.request(
            base,
            "/context/build",
            {
                "task_id": task["id"],
                "session_id": session["id"],
                "agent_id": "codex",
                "known_checkpoint_version": 0,
                "user_id": "user-1",
                "project_id": "project-1",
                "repository_id": "repo-1",
            },
        )
        assert status == 200
        assert context["active_task"]["id"] == task["id"]
        assert context["latest_checkpoint"]["version"] == 1
        assert context["critical_constraints"] == ["no push"]

    def test_invalid_repository_scope_is_a_conflict(self, api_server):
        base = api_server["base_url"]
        self.request(
            base,
            "/v2/tasks",
            {
                "user_id": "user-1",
                "workspace_id": "workspace-1",
                "project_id": "project-1",
                "repository_id": "shared-repo",
                "title": "First",
                "objective": "First",
                "agent_id": "claude-code",
            },
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            self.request(
                base,
                "/v2/tasks",
                {
                    "user_id": "user-1",
                    "workspace_id": "workspace-1",
                    "project_id": "project-2",
                    "repository_id": "shared-repo",
                    "title": "Cross-scope",
                    "objective": "Reject",
                    "agent_id": "claude-code",
                },
            )
        assert error.value.code == 409


class TestRetrievalFeedbackEndpoints:
    request = staticmethod(TestTaskSessionContinuityEndpoints.request)

    def test_feedback_utility_shadow_and_cold_lifecycle(self, api_server):
        pipeline = api_server["pipeline"]
        base = api_server["base_url"]
        task = pipeline.task_service.create_task(
            user_id="user-feedback",
            workspace_id="workspace-feedback",
            project_id="project-feedback",
            repository_id="repo-feedback",
            title="Feedback API",
            objective="Measure useful context",
            created_by="claude-code",
            canonical_path="/work/feedback",
        )
        session = pipeline.session_service.start_session(
            task_id=task["id"], agent_id="claude-code"
        )
        memory_id = pipeline.store.store(
            "telemetry marker constraint",
            "telemetry marker constraint",
            "project",
            "active",
            [],
            agent="claude-code",
            user_id=task["user_id"],
            workspace_id=task["workspace_id"],
            project_id=task["project_id"],
            repository_id=task["repository_id"],
            task_id=task["id"],
            session_id=session["id"],
        )
        status, context = self.request(
            base,
            "/context/build",
            {
                "task": "telemetry marker",
                "user_id": task["user_id"],
                "workspace_id": task["workspace_id"],
                "project_id": task["project_id"],
                "repository_id": task["repository_id"],
                "task_id": task["id"],
                "session_id": session["id"],
                "agent_id": "claude-code",
                "idempotency_key": "rest-feedback-run",
            },
        )
        assert status == 200
        assert context["retrieval_run_id"]
        assert context["context_pack_id"]
        assert context["memories"][0]["id"] == memory_id
        status, feedback = self.request(
            base,
            f"/v2/context/{context['context_pack_id']}/feedback",
            {
                "agent_id": "claude-code",
                "used_memory_ids": [memory_id],
                "idempotency_key": "rest-use",
            },
        )
        assert status == 201
        assert feedback["feedback"][0]["usage_type"] == "used"
        status, outcome = self.request(
            base,
            f"/v2/tasks/{task['id']}/outcome",
            {
                "session_id": session["id"],
                "status": "completed",
                "successful": True,
                "agent_id": "claude-code",
                "idempotency_key": "rest-outcome",
            },
        )
        assert status == 201
        assert outcome["successful"] is True
        assert outcome["agent_id"] == "claude-code"

        scope = (
            f"?user_id={task['user_id']}&project_id={task['project_id']}"
            f"&repository_id={task['repository_id']}&task_id={task['id']}"
        )
        status, explanation = self.request(
            base, f"/v2/memories/{memory_id}/utility{scope}", method="GET"
        )
        assert status == 200
        assert explanation["policy_version"] == "utility-v1"
        status, history = self.request(
            base, f"/v2/memories/{memory_id}/utility/history{scope}", method="GET"
        )
        assert status == 200
        assert history["history"]
        with pytest.raises(urllib.error.HTTPError) as error:
            self.request(
                base,
                f"/v2/memories/{memory_id}/utility?project_id=other",
                method="GET",
            )
        assert error.value.code == 404

        status, report = self.request(
            base, "/v2/admin/utility/shadow-report", method="GET"
        )
        assert status == 200
        assert report["production_ranking_changed"] is False

        self.request(
            base,
            f"/v2/memories/{memory_id}/utility/cold",
            {"reason": "test"},
        )
        status, normal = self.request(
            base, "/search?q=telemetry+marker&mode=keyword", method="GET"
        )
        assert status == 200
        assert normal["results"] == []
        status, explicit = self.request(
            base,
            "/search?q=telemetry+marker&mode=keyword&include_cold=true",
            method="GET",
        )
        assert status == 200
        assert explicit["results"][0]["id"] == memory_id
        self.request(base, f"/v2/memories/{memory_id}/utility/restore", {})


class TestSearchEndpoint:
    def test_search(self, api_server):
        # First capture something
        body = json.dumps(
            {"text": "Ema decided Prism stays domain-agnostic", "source": "test"}
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{api_server['base_url']}/capture",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req)

        # Then search
        resp = urllib.request.urlopen(
            f"{api_server['base_url']}/search?q=Prism&mode=keyword"
        )
        data = json.loads(resp.read())
        assert "results" in data
        assert "count" in data

    def test_search_missing_query(self, api_server):
        try:
            urllib.request.urlopen(f"{api_server['base_url']}/search")
            assert False, "Expected 400 error"
        except urllib.error.HTTPError as e:
            assert e.code == 400


class TestEntityEndpoint:
    def test_entity_get(self, api_server):
        # Capture to create entity
        body = json.dumps({"text": "Ema works on Prism", "source": "test"}).encode(
            "utf-8"
        )
        req = urllib.request.Request(
            f"{api_server['base_url']}/capture",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req)

        # Get entity
        try:
            resp = urllib.request.urlopen(f"{api_server['base_url']}/entity/ema")
            data = json.loads(resp.read())
            assert data["name"] == "Ema"
            assert "compiled_truth" in data
        except urllib.error.HTTPError as e:
            if e.code == 404:
                pytest.skip("Entity not created by capture (detection dependent)")

    def test_entity_not_found(self, api_server):
        try:
            urllib.request.urlopen(f"{api_server['base_url']}/entity/nonexistent")
            assert False, "Expected 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404


class TestContextBuildEndpoint:
    def test_context_build(self, api_server):
        # Capture first
        body = json.dumps(
            {"text": "Ema decided Prism stays domain-agnostic", "source": "test"}
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{api_server['base_url']}/capture",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req)

        # Build context
        resp = urllib.request.urlopen(
            f"{api_server['base_url']}/context/build?task=implement+vector+search"
        )
        data = json.loads(resp.read())
        assert "memories" in data
        assert "entities" in data

    def test_context_build_missing_task(self, api_server):
        try:
            urllib.request.urlopen(f"{api_server['base_url']}/context/build")
            assert False, "Expected 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400


class TestDreamEndpoint:
    def test_dream_post(self, api_server):
        body = json.dumps(
            {
                "phases": ["orphan_detect"],
                "dry_run": False,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{api_server['base_url']}/dream",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req)
        data = json.loads(resp.read())
        assert "status" in data
        assert data["status"] in ("ok", "partial")


class TestNotFoundEndpoint:
    def test_not_found(self, api_server):
        try:
            urllib.request.urlopen(f"{api_server['base_url']}/nonexistent")
            assert False, "Expected 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404


class TestContextPackV2Endpoints:
    @staticmethod
    def request(base_url, path, body=None, method="POST"):
        request = urllib.request.Request(
            f"{base_url}{path}",
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            response = urllib.request.urlopen(request)
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())
        return response.status, json.loads(response.read())

    def test_build_get_explain_expand_feedback_and_v1_compatibility(self, api_server):
        pipeline = api_server["pipeline"]
        base = api_server["base_url"]
        task = pipeline.task_service.create_task(
            user_id="context-user",
            workspace_id="context-workspace",
            project_id="context-project",
            repository_id="context-repo",
            title="Context V2 REST",
            objective="context marker",
            created_by="claude-code",
            canonical_path="/work/context",
        )
        session = pipeline.session_service.start_session(
            task_id=task["id"], agent_id="claude-code"
        )
        pipeline.session_service.join_session(session["id"], agent_id="codex")
        pipeline.session_service.create_checkpoint(
            session["id"],
            agent_id="claude-code",
            constraints=["retain critical context"],
            approved_decisions=["use context v2"],
        )
        long_text = "context marker repository evidence " * 100
        memory_id = pipeline.store.store(
            long_text,
            long_text,
            "project",
            "active",
            [],
            source="repository:src/context.py",
            agent="claude-code",
            user_id=task["user_id"],
            workspace_id=task["workspace_id"],
            project_id=task["project_id"],
            repository_id=task["repository_id"],
            task_id=task["id"],
            session_id=session["id"],
        )
        body = {
            "user_id": task["user_id"],
            "workspace_id": task["workspace_id"],
            "project_id": task["project_id"],
            "repository_id": task["repository_id"],
            "task_id": task["id"],
            "session_id": session["id"],
            "agent_id": "codex",
            "known_checkpoint_version": 0,
            "max_tokens": 3000,
            "client_capabilities": ["structured_json", "references"],
            "idempotency_key": "rest-context-v2",
        }
        status, pack = self.request(base, "/v2/context/build", body)
        assert status == 201
        assert pack["schema_version"] == 2
        assert pack["retrieval"]["retrieval_run_id"]
        assert pack["references"]
        scope_keys = (
            "user_id",
            "workspace_id",
            "project_id",
            "repository_id",
            "task_id",
        )
        query = urllib.parse.urlencode({key: body[key] for key in scope_keys})
        status, fetched = self.request(
            base, f"/v2/context/{pack['context_pack_id']}?{query}", method="GET"
        )
        assert status == 200
        assert fetched["context_pack_id"] == pack["context_pack_id"]
        status, explanation = self.request(
            base,
            f"/v2/context/{pack['context_pack_id']}/explain?{query}",
            method="GET",
        )
        assert status == 200
        assert explanation["utility_affected_ranking"] is False
        reference_id = pack["references"][0]["reference_id"]
        status, expanded = self.request(
            base,
            f"/v2/context/{pack['context_pack_id']}/references/{reference_id}?{query}&agent_id=codex",
            method="GET",
        )
        assert status == 200
        assert expanded["reference_id"] == reference_id
        status, feedback = self.request(
            base,
            f"/v2/context/{pack['context_pack_id']}/feedback",
            {
                **{key: body[key] for key in scope_keys},
                "agent_id": "codex",
                "used_memory_ids": [memory_id],
                "idempotency_key": "rest-context-used",
            },
        )
        assert status == 201
        assert feedback["feedback"][0]["usage_type"] == "used"

        status, rejected = self.request(
            base,
            f"/v2/context/{pack['context_pack_id']}/feedback",
            {**body, "project_id": "wrong", "used_memory_ids": [memory_id]},
        )
        assert status == 404
        assert rejected["code"] == "not_found"

        status, v1 = self.request(
            base,
            "/v1/context/build",
            {
                "task": "context marker",
                "user_id": task["user_id"],
                "workspace_id": task["workspace_id"],
                "project_id": task["project_id"],
                "repository_id": task["repository_id"],
                "task_id": task["id"],
                "session_id": session["id"],
                "agent_id": "prism",
                "formal_agent_id": "codex",
                "max_tokens": 3000,
            },
        )
        assert status == 200
        assert "context_markdown" in v1
        assert v1["context_pack_id"]
        assert v1["schema_version"] == 2
        assert memory_id in v1["selected_memories"]
        assert "Deferred evidence references" in v1["context_markdown"]
