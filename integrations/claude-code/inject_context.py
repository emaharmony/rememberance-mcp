#!/usr/bin/env python3
"""Claude Code SessionStart hook — inject recalled memory.

Reads the SessionStart hook event from stdin, runs a fast keyword search for
the current project, and prints the result as `additionalContext` so Claude
Code injects it before the first model turn.

Uses keyword search (not the balanced/vector context builder) so it stays
sub-second and never depends on Ollama — important because this hook runs
synchronously before the session starts.

Pure stdlib — runs under any Python 3. Never blocks a session: any failure
(Remembrance down, bad JSON, timeout) exits 0 with no output.

Env:
  REMEMBRANCE_URL   base URL of the Remembrance service (default 127.0.0.1:18790)
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


def _project_from_cwd(cwd: str) -> str:
    name = os.path.basename((cwd or "").rstrip("/\\"))
    return name or "claude-code"


def _format(results: list[dict], project: str) -> str:
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
