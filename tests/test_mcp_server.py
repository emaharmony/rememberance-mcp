"""Smoke test for the MCP stdio server.

Guards the regression where `python -m recall_mcp` crashed on startup
because `Server.run()` was called without its stdio streams and init options —
which meant no MCP client (Claude Code, etc.) could ever connect.

The test launches the real module over stdio, completes the MCP handshake, and
asserts the expected memory tools are advertised. Skipped if the optional `mcp`
package isn't installed.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile

import pytest

mcp_client = pytest.importorskip("mcp.client.stdio")
from mcp.client.stdio import stdio_client, StdioServerParameters  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402

EXPECTED_TOOLS = {
    "memory_capture",
    "memory_search",
    "memory_context_build",
    "memory_dream",
    "recall_task_create",
    "recall_session_start",
    "recall_session_join",
    "recall_session_checkpoint",
    "recall_session_delta",
    "recall_context_feedback",
    "recall_task_outcome",
    "recall_memory_utility",
}


def test_mcp_server_starts_and_lists_tools():
    home = tempfile.mkdtemp(prefix="recall-mcp-test-")

    async def _run():
        env = dict(os.environ)
        # Isolate state and force the fast heuristic gate so the server boots
        # quickly and touches no real data.
        env["RECALL_HOME"] = home
        env["RECALL_GATE_BACKENDS"] = "heuristic"
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "recall_mcp"],
            env=env,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                task_result = await session.call_tool(
                    "recall_task_create",
                    {
                        "user_id": "user-1",
                        "workspace_id": "workspace-1",
                        "project_id": "project-1",
                        "repository_id": "repo-1",
                        "title": "MCP handoff",
                        "objective": "Share state",
                        "agent_id": "claude-code",
                        "canonical_path": "/work/recall",
                        "idempotency_key": "mcp-task",
                    },
                )
                task = json.loads(task_result.content[0].text)
                session_result = await session.call_tool(
                    "recall_session_start",
                    {
                        "task_id": task["id"],
                        "agent_id": "claude-code",
                        "idempotency_key": "mcp-session",
                    },
                )
                shared_session = json.loads(session_result.content[0].text)
                await session.call_tool(
                    "recall_session_checkpoint",
                    {
                        "session_id": shared_session["id"],
                        "agent_id": "claude-code",
                        "summary": "Claude checkpoint",
                        "completed": ["schema"],
                        "remaining": ["API"],
                        "constraints": ["no push"],
                        "idempotency_key": "mcp-checkpoint",
                    },
                )
                await session.call_tool(
                    "recall_session_join",
                    {
                        "session_id": shared_session["id"],
                        "agent_id": "codex",
                    },
                )
                delta_result = await session.call_tool(
                    "recall_session_delta",
                    {
                        "session_id": shared_session["id"],
                        "known_version": 0,
                        "agent_id": "codex",
                    },
                )
                delta = json.loads(delta_result.content[0].text)
                outcome_result = await session.call_tool(
                    "recall_task_outcome",
                    {
                        "task_id": task["id"],
                        "session_id": shared_session["id"],
                        "status": "completed",
                        "successful": True,
                        "agent_id": "codex",
                        "idempotency_key": "mcp-outcome",
                    },
                )
                outcome = json.loads(outcome_result.content[0].text)
                return {t.name for t in tools.tools}, delta, outcome

    try:
        names, delta, outcome = asyncio.run(asyncio.wait_for(_run(), timeout=60))
    finally:
        shutil.rmtree(home, ignore_errors=True)
    assert EXPECTED_TOOLS.issubset(names), f"missing tools: {EXPECTED_TOOLS - names}"
    assert len(names) >= 11
    assert delta["checkpoint"]["objective"] == "Share state"
    assert delta["checkpoint"]["constraints"] == ["no push"]
    assert outcome["successful"] is True
    assert outcome["agent_id"] == "codex"
