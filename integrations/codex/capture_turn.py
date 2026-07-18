#!/usr/bin/env python3
"""Codex Stop hook — capture the turn into Recall.

Codex's Stop event hands the hook `last_assistant_message` directly (it does
NOT pass a transcript path like Claude Code does), so this captures that final
message per turn and forwards it to Recall's /capture endpoint. The gate
decides what is worth keeping (SKIP/COLD/ACTIVE/PERSIST), so it is safe to
forward every turn — trivial ones are dropped, and one message per Stop means
captures are naturally deduplicated without a cursor.

Capture is FIRE-AND-FORGET: the hook builds the payload, spawns a detached
worker to do the (potentially slow) HTTP POST, and returns immediately so it
never makes you wait at the end of a turn.

Pure stdlib — runs under any Python 3. Never blocks a session: any failure
exits 0.

Env:
  RECALL_URL      base URL of the service (default 127.0.0.1:18790)
  RECALL_TIMEOUT  HTTP timeout seconds (default 30)
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from compat_env import get_env, resolve_home  # noqa: E402

RECALL_URL = get_env("URL", "http://127.0.0.1:18790").rstrip("/")
TIMEOUT = float(get_env("TIMEOUT", "30"))
HOME = resolve_home()
OUTBOX_DIR = HOME / ".codex_outbox"
MIN_CHARS = 20


def _project_from_cwd(cwd: str) -> str:
    name = os.path.basename((cwd or "").rstrip("/\\"))
    return name or "codex"


def _send(payload_path: str) -> int:
    """Detached worker: POST the payload file to /capture, then remove it."""
    try:
        with open(payload_path, "rb") as f:
            data = f.read()
        req = urllib.request.Request(
            RECALL_URL + "/capture",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=TIMEOUT).read()
    except Exception as exc:
        if os.environ.get("REM_DEBUG"):
            sys.stderr.write(f"recall capture failed: {type(exc).__name__}: {exc}\n")
    finally:
        try:
            os.remove(payload_path)
        except Exception:
            pass
    return 0


def _spawn_worker(payload_path: str) -> None:
    """Launch a detached copy of this script to do the POST, then return.

    On Windows the calling hook process is often inside a Job Object that kills
    its children when it exits. Since the hook returns immediately, the worker
    must break away from that job (CREATE_BREAKAWAY_FROM_JOB) or it is killed
    mid-request. If breakaway is not permitted, fall back to a plain detached
    spawn, and finally to a blocking send.
    """
    cmd = [sys.executable, os.path.abspath(__file__), "--send", payload_path]
    out = subprocess.DEVNULL
    if os.environ.get("REM_DEBUG"):
        try:
            out = open(str(OUTBOX_DIR / "worker.log"), "a", encoding="utf-8")
        except Exception:
            out = subprocess.DEVNULL
    base = {
        "stdin": subprocess.DEVNULL,
        "stdout": out,
        "stderr": subprocess.STDOUT,
    }
    if os.name == "nt":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_BREAKAWAY_FROM_JOB = 0x01000000
        base["close_fds"] = True
        attempts = [
            DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB,
            DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        ]
        for flags in attempts:
            try:
                subprocess.Popen(cmd, creationflags=flags, **base)
                return
            except Exception:
                continue
        _send(payload_path)  # last resort: blocking
        return

    try:
        subprocess.Popen(cmd, start_new_session=True, **base)
    except Exception:
        _send(payload_path)


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--send":
        return _send(sys.argv[2])

    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0

    text = (event.get("last_assistant_message") or "").strip()
    if len(text) < MIN_CHARS:
        return 0

    project = _project_from_cwd(event.get("cwd", ""))
    payload = json.dumps(
        {
            "text": text,
            "source": f"codex:{project}",
            "category": project,
        }
    )

    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    fd, payload_path = tempfile.mkstemp(suffix=".json", dir=str(OUTBOX_DIR))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(payload)

    _spawn_worker(payload_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
