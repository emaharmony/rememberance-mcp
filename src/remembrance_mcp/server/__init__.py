"""Server package — runtime entry points.

Groups the process entry points that were previously loose modules at the
package root:
  - ``mcp.py``      — the MCP (Model Context Protocol) stdio server
  - ``serve.py``    — the REST API + optional NATS subscriber launcher
  - ``nats_sub.py`` — the NATS subscriber that auto-captures agent output

``create_server`` is re-exported here so ``from remembrance_mcp.server import
create_server`` keeps working after the move from the old single
``remembrance_mcp.server`` module to this package.
"""

from remembrance_mcp.server.mcp import create_server  # noqa: F401
