#!/usr/bin/env python3
"""Codex SessionStart hook — inject recalled memory.

Codex fires SessionStart on `startup`, `resume`, `clear`, and `compact`. This
hook runs a fast keyword search for the current project and returns it as
`hookSpecificOutput.additionalContext` so Codex injects it as developer context
before the first model turn. The output shape is identical to the Claude Code
SessionStart contract, so this mirrors `claude-code/inject_context.py`.

We only inject on `startup` and `resume` — `clear`/`compact` already carry the
session's own context and re-injecting there is noise.

Pure stdlib — runs under any Python 3. Never blocks a session: any failure
(Remembrance down, bad JSON, timeout) exits 0 with no output.

Env:
  REMEMBRANCE_URL          base URL of the service (default 127.0.0.1:18790)
  REMEMBRANCE_TIMEOUT      HTTP timeout seconds (default 6)
  REMEMBRANCE_INJECT_LIMIT max memories to inject (default 8)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

REMEMBRANCE_URL = os.environ.get("REMEMBRANCE_URL", "http://127.0.0.1:18790").rstrip("/")
TIMEOUT = float(os.environ.get("REMEMBRANCE_TIMEOUT", "6"))
LIMIT = int(os.environ.get("REMEMBRANCE_INJECT_LIMIT", "8"))
INJECT_SOURCES = {"startup", "resume"}


def _project_from_cwd(cwd: str) -> str:
    name = os.path.basename((cwd or "").rstrip("/\\"))
    return name or "codex"


def _format(results: list, project: str) -> str:
    lines = []
    for m in results:
        text = (m.get("summary") or m.get("content") or "").strip()
        if not text:
            continue
        tag = m.get("category") or m.get("tier") or ""
        suffix = f" _({tag})_" if tag else ""
        lines.append(f"- {text}{suffix}")
    if not lines:
        return ""
    return f"## Remembrance — recalled memory for **{project}**\n\n" + "\n".join(lines)


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        event = {}

    # Codex SessionStart source: startup|resume|clear|compact
    source = event.get("source", "startup")
    if source not in INJECT_SOURCES:
        return 0

    project = _project_from_cwd(event.get("cwd", ""))
    query = urllib.parse.urlencode({"q": project, "mode": "keyword", "limit": str(LIMIT)})

    try:
        with urllib.request.urlopen(f"{REMEMBRANCE_URL}/search?{query}", timeout=TIMEOUT) as resp:
            data = json.load(resp)
    except Exception:
        return 0  # memory is best-effort; never block the session

    markdown = _format(data.get("results", []) or [], project)
    if not markdown:
        return 0

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": markdown,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
