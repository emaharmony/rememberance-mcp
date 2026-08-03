# Stop-hook diagnostics

The repository-owned Claude Code Stop hook is
`integrations/claude-code/capture_transcript.py`. It is best-effort: malformed
input and an unavailable Recall endpoint return exit code zero after sanitized
local diagnostics. A regression test covers malformed input.

The previously reported `Stop hook exited with code 1` was not reproducible.
The configured global hook invokes the repository virtual-environment Python
and the script above; both malformed JSON and an unreachable local endpoint
returned zero during the Phase 6 gate. No retained Claude runner log identified
the original failure, so external runner/configuration uncertainty remains.
Do not suppress a future failure: capture the client debug log, hook command,
Python path, sanitized input shape, and stderr before changing the hook.
