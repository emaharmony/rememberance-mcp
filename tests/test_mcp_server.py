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
                return {t.name for t in tools.tools}

    try:
        names = asyncio.run(asyncio.wait_for(_run(), timeout=60))
    finally:
        shutil.rmtree(home, ignore_errors=True)
    assert EXPECTED_TOOLS.issubset(names), f"missing tools: {EXPECTED_TOOLS - names}"
    assert len(names) >= 11
