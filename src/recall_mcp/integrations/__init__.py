"""Agent adapters (Claude Code, Codex, ...) packaged with recall_mcp.

Everything under this package ships inside the `recall-mcp` wheel, so
`pip install recall-mcp` is enough to get the hook scripts and their
non-Python assets (settings snippets, hook manifests, launcher wrappers) on
any machine -- no source checkout and no per-installation hand-wiring
required.

`asset_path()` is the supported way to locate those packaged, non-Python
assets at runtime (e.g. from the forthcoming install CLI). It is built on
`importlib.resources` rather than `__file__` arithmetic so it keeps working
regardless of whether recall_mcp is installed as a built wheel, in editable
mode, or (in principle) as a zipped package.
"""

from __future__ import annotations

import atexit
import importlib.resources
from contextlib import ExitStack
from pathlib import Path

__all__ = ["asset_path"]

# Backs `asset_path()`'s zip-safe fallback: `importlib.resources.as_file()`
# extracts zipped resources to a temporary path for the lifetime of a `with`
# block. Callers need a plain `Path` they can hold onto (e.g. to pass to a
# subprocess or a JSON hook command), not a context manager, so the contexts
# are kept open on this module-level stack and only torn down at interpreter
# exit -- the standard pattern recommended by the `importlib.resources` docs
# for long-lived callers that can't use the `with as_file(...)` form directly.
_asset_contexts = ExitStack()
atexit.register(_asset_contexts.close)


def asset_path(*parts: str) -> Path:
    """Return a real filesystem `Path` to a packaged integration asset.

    `parts` are joined under this package, e.g.
    `asset_path("claude_code", "settings.snippet.json")` or
    `asset_path("codex", "hooks.json")`.

    Works whether recall_mcp is installed as a wheel, editable, or zipped.
    In the zipped case the resource is extracted to a temporary location
    that persists for the life of the process and is cleaned up at exit.
    """
    resource = importlib.resources.files(__name__)
    for part in parts:
        resource = resource / part
    real_path = _asset_contexts.enter_context(importlib.resources.as_file(resource))
    return Path(real_path)
