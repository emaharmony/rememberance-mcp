"""Deprecated location -- re-exports `recall_mcp.integrations.compat_env`.

The real implementation now ships inside the installed `recall_mcp` package.
This module only exists so anything still importing `compat_env` from this
old checkout-relative path (e.g. via a hand-rolled `sys.path` insert) keeps
working; switch to `from recall_mcp.integrations.compat_env import ...`
when convenient.
"""

from __future__ import annotations

import sys

print(
    "integrations/compat_env.py is deprecated; "
    "import recall_mcp.integrations.compat_env instead. Delegating for now.",
    file=sys.stderr,
)

from recall_mcp.integrations.compat_env import get_env, resolve_home  # noqa: F401,E402
