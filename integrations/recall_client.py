"""Deprecated location -- re-exports `recall_mcp.integrations.recall_client`.

The real implementation now ships inside the installed `recall_mcp` package.
This module only exists so anything still importing `recall_client` from
this old checkout-relative path (e.g. via a hand-rolled `sys.path` insert)
keeps working; switch to
`from recall_mcp.integrations import recall_client` when convenient.
"""

from __future__ import annotations

import sys

print(
    "integrations/recall_client.py is deprecated; "
    "import recall_mcp.integrations.recall_client instead. Delegating for now.",
    file=sys.stderr,
)

from recall_mcp.integrations.recall_client import (  # noqa: F401,E402
    derive_scope,
    deliver_context,
    ensure_task,
    load_client_state,
    new_client_state,
    render_context,
    save_client_state,
)
