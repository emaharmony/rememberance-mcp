# Autonomous Loop: Claude Code Runner for Phases 0–4

## The prompt (save as `LOOP_PROMPT.md` in repo root)

```markdown
You are working autonomously on the recall-mcp repository. You get ONE
bounded work session. Other sessions ran before you and will run after you.
Your ONLY shared memory is the files listed below. Follow this protocol exactly.

## Protocol — do these in order, no exceptions

1. READ STATE FIRST
   - Read `PROGRESS.md` (current phase, current task, last session's outcome).
   - Read `docs/roadmap.md`, then the design doc for the current phase
     (`docs/semantic-retrieval.md` or `docs/deferred-hardening.md`).
   - Run `git log --oneline -10` and `git status` to see actual repo state.
   - If `BLOCKED.md` exists: STOP immediately. Output its contents and exit.
     Do not attempt to work around a block.

2. VERIFY BASELINE BEFORE TOUCHING ANYTHING
   - Run the full test suite (`python -m pytest -q`).
   - If tests FAIL and PROGRESS.md says they passed last session: your ONLY
     task this session is diagnosing and fixing that regression. Do nothing else.

3. SELECT EXACTLY ONE TASK
   - Take the next unchecked task for the current phase in `PROGRESS.md`.
   - Scope: completable in one session (~one module, one wiring step, or one
     test file). If the next task is too big, split it into subtasks in
     PROGRESS.md and take the first.
   - You work ONLY on Phases 0–4. If Phase 4 is complete, write BLOCKED.md
     stating "Phases 0–4 complete; Phases 5+ need human review" and stop.

4. IMPLEMENT
   - Follow the design doc for this task exactly. Where the doc specifies an
     interface, match it. Do not improvise architecture.
   - Write the tests for this task in the SAME session (design docs list them).
   - No new required dependencies; optional deps go behind extras per the
     existing pattern.

5. VERIFY — tests are the arbiter, not your judgment
   - Run the FULL suite, not just new tests. All green or the task is not done.
   - If you cannot get green after 3 distinct approaches: revert your changes
     (`git checkout .`), write BLOCKED.md describing what you tried and why it
     failed, and stop. A clean revert beats a broken commit.

6. COMMIT + UPDATE STATE (only if green)
   - `git add -A && git commit -m "phase-N: <task> (autonomous session)"`
   - Update PROGRESS.md: check off the task, record date, test count,
     and one line of context the NEXT session needs.

## Hard rules
- NEVER mark a task done with failing tests.
- NEVER delete or weaken an existing test to get green.
- NEVER make product decisions the docs defer to a human (privacy scope
  defaults, chunk sizing beyond doc defaults, Postgres cutover). Write
  BLOCKED.md instead and stop.
- NEVER work on more than one task per session.
- If PROGRESS.md is missing, create it from roadmap.md Phases 0–4 as a
  checklist, commit it, and stop — that was your session.
```

## The loop (run on your machine)

```bash
#!/bin/bash
# loop.sh — run from repo root
while true; do
  # Stop looping the moment the agent raises a blocker
  if [ -f BLOCKED.md ]; then
    echo "=== BLOCKED — human input needed ==="; cat BLOCKED.md; break
  fi
  claude -p "$(cat LOOP_PROMPT.md)" --allowedTools "Edit,Write,Bash,Read"
  sleep 600   # 10 minutes
done
```

Notes:
- `claude -p` is headless mode. Scope allowed tools rather than using
  `--dangerously-skip-permissions`; an unattended agent with unrestricted
  bash is a real risk.
- Run it in a dedicated branch (`git checkout -b autonomous-work`) so you
  review one PR at the end, and a bad run never touches main.
- The 10-minute sleep is a pause BETWEEN sessions; each session runs to
  completion regardless of length. That's the correct unit — "one task,
  verified" — not a wall-clock slice.

## Why this converges instead of drifting
- **State file, not memory:** every session rebuilds context from
  PROGRESS.md + git, so session 14 knows exactly what session 13 did.
- **Tests as arbiter:** progress is gated on green, so errors can't
  compound silently across sessions.
- **One task per session:** small diffs, each committed, each revertable.
- **Escalation over improvisation:** BLOCKED.md turns "agent guesses on a
  product decision at 3am" into "loop halts and asks you."
```

---

## Tuned configuration (optimal parameters)

### Prompt additions (append to LOOP_PROMPT.md protocol)

```markdown
7. PHASE GATE
   - On completing the LAST task of a phase: write `PHASE_COMPLETE.md`
     naming the phase and summarizing what shipped, then STOP. A human
     reviews the diff, deletes the file, and relaunches the loop.

8. CONTEXT ECONOMY
   - Keep PROGRESS.md under ~100 lines: collapse completed phases to a
     single summary line; only the active phase stays expanded as a
     task checklist.
```

### Tuned loop.sh

```bash
#!/bin/bash
# loop.sh — run from repo root on a dedicated branch
set -u
MAX_SESSIONS=30        # hard ceiling; past this, something is wrong
MAX_FAIL_STREAK=3      # consecutive non-zero exits → structural problem
SLEEP_BETWEEN=60       # breather, not a throttle; sessions are task-driven

session=0; fail_streak=0
git checkout -B autonomous-work

while [ "$session" -lt "$MAX_SESSIONS" ]; do
  # Human-input gates
  if [ -f BLOCKED.md ];        then echo "=== BLOCKED ===";        cat BLOCKED.md;        break; fi
  if [ -f PHASE_COMPLETE.md ]; then echo "=== PHASE DONE — review, delete file, relaunch ==="; cat PHASE_COMPLETE.md; break; fi

  session=$((session+1))
  echo "=== session $session / $MAX_SESSIONS ==="

  claude -p "$(cat LOOP_PROMPT.md)" \
    --allowedTools "Read" "Grep" "Glob" "Edit" "Write" \
                   "Bash(python -m pytest *)" "Bash(python *)" \
                   "Bash(git add *)" "Bash(git commit *)" "Bash(git status)" \
                   "Bash(git log *)" "Bash(git checkout .)" "Bash(git diff *)" \
                   "Bash(pip install *)" "Bash(ruff *)" \
    --max-turns 80 \
    --max-budget-usd 3 \
    --output-format json \
    > "loop-logs/session-$session.json" 2> "loop-logs/session-$session.err"

  if [ $? -ne 0 ]; then
    fail_streak=$((fail_streak+1))
    echo "session failed ($fail_streak/$MAX_FAIL_STREAK)"
    [ "$fail_streak" -ge "$MAX_FAIL_STREAK" ] && { echo "=== FAIL STREAK — stopping ==="; break; }
  else
    fail_streak=0
  fi

  sleep "$SLEEP_BETWEEN"
done
```
(`mkdir -p loop-logs` first; add `loop-logs/` to `.gitignore`.)

### Parameter rationale

| Parameter | Value | Why |
|---|---|---|
| sleep | 60s | Breather only. Cadence is task-driven; each session runs to completion. A fixed 10-min sleep optimizes nothing. |
| `--max-turns` | 80 | Healthy task ≈ 20–50 turns. 80 = thrashing; better to die and let the next session revert + BLOCK. |
| `--max-budget-usd` | 3/session | Cost of a mistake you can tolerate, not the happy path. Belt-and-suspenders with max-turns. |
| fail streak | 3 | One failure is transient; three consecutive is structural. Stop re-discovering the same wall. |
| session ceiling | 30 | Phases 0–4 ≈ 18–24 tasks. Past 30, eyes-on beats iteration. |
| model | strongest available | Unattended inverts the tradeoff: per-mistake cost >> per-token cost. |
| review cadence | per phase (PHASE_COMPLETE.md) | Five 10-min check-ins vs. one giant end review = compounding-error insurance. |
| tool scope | allowlist above | `Bash(python -m pytest *)` style scoped rules; no `--dangerously-skip-permissions` unattended. |
| output | `--output-format json` | Structured result + cost per session in logs; stderr is the progress narrative. |

**Philosophy: optimize for error containment, not speed.** Throughput is never the bottleneck of an autonomous loop; confidently compounding a mistake across sessions is. Every parameter above is a bulkhead.
