#!/usr/bin/env python3
"""Deprecated location -- delegates to the packaged Claude Code hook.

The real implementation now ships inside the installed `recall_mcp` package
at `recall_mcp.integrations.claude_code.capture_transcript`, so `pip install
recall-mcp` is enough to get this hook on any machine, without a source
checkout. This file only exists so hook registrations still pointing at this
old checkout-relative path keep working; update your Claude Code
`settings.json` (or re-run the installer) to point at the packaged module
when convenient.
"""

from __future__ import annotations

import sys

print(
    "integrations/claude-code/capture_transcript.py is deprecated; "
    "the implementation now ships inside the recall_mcp package "
    "(recall_mcp.integrations.claude_code.capture_transcript). "
    "Delegating for now.",
    file=sys.stderr,
)

from recall_mcp.integrations.claude_code.capture_transcript import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
