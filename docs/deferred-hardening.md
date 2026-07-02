# Plan: Deferred Hardening (Exception Handling + Type Hints + Tooling)

**Status:** Planned for a future session — NOT part of the semantic-retrieval work.
**Why separate:** Bundling a codebase-wide exception/type pass into the vector-search feature would make that diff hard to review and hard to roll back. These are cross-cutting hygiene changes and deserve their own session and their own PR(s).

---

## Concern A: Broad / silent exception handling

30 `except Exception` / bare-`except` blocks across 12 files. The risk is specific to a memory system: a silently-swallowed write or lookup looks like "the memory just forgot" — a data-integrity bug masquerading as normal behavior.

### Triage by risk

**Tier 1 — data-integrity paths (fix first).** Silently dropping here corrupts or loses memory:
- `search/hybrid.py` — L156, L384, L402, L419, L433 (five blocks; retrieval correctness. Note L402/L419 are the batch entity-fetch swallowers).
- `store/memory.py` — L174, L196, L311 (store internals).
- `pipeline.py` — L88, L191 (capture path; L191 graph-wiring is intentionally non-blocking but should still log specifics).

**Tier 2 — service/IO boundaries (fix second).** Broad catch is more defensible (external systems fail unpredictably) but should still log and narrow:
- `gate/ollama.py` L111, L209; `gate_backends.py` L345, L470; `api/rest.py` L241, L315, L336, L385; `nats_sub.py` (6 blocks); `store/markdown.py` L103; `registry.py` L154.

**Tier 3 — startup/shutdown.** Lowest risk:
- `server.py` L458; `serve.py` L71; `dream/cycle.py` L133, L430.

### Fix pattern (per block)
1. Narrow to the exception(s) actually expected (`sqlite3.Error`, `json.JSONDecodeError`, `httpx`/`requests` errors, `OSError`, etc.).
2. Never swallow silently — log at `warning` (recoverable) or `error` (data-affecting) with the exception detail and enough context to identify the record/operation.
3. Re-raise where the caller cannot safely continue (especially Tier 1 writes).
4. Keep deliberately non-blocking catches (e.g. graph wiring), but make the log line explicit that failure was tolerated on purpose.

### Suggested sequencing
One PR per tier, Tier 1 first. Add a regression test per data-integrity block proving the failure is now logged/surfaced rather than hidden.

---

## Concern B: Type-hint coverage + type checker

20 of 32 files have return hints. Real targets (excluding empty `__init__.py` files, which have nothing to type):
- `src/remembrance_mcp/server.py`
- `src/remembrance_mcp/serve.py`
- `src/remembrance_mcp/__main__.py`

### Plan
1. Add return + parameter hints to the three modules above and any partially-typed functions elsewhere.
2. Add `mypy` (or `pyright`) config to `pyproject.toml`. Start lenient (`ignore_missing_imports = true` for the optional heavy deps behind extras), then tighten.
3. Wire the type check into CI (see Concern C) so coverage can't regress.

---

## Concern C: Tooling (enables A & B to stick)

These weren't in the original feature scope but are what make the hardening durable:

1. **CI** — `.github/workflows/` is absent. Add GitHub Actions running the test suite on Python 3.10–3.12 across the minimal and `[all]` extras. Single biggest durability win.
2. **Lint/format** — no `ruff`/`black` config present. Add `ruff` (lint + format) config to `pyproject.toml` + a pre-commit hook.
3. **Coverage gate** — `pytest-cov` with a fail-under threshold (~80%), surfacing thin spots (notably `test_mcp_server.py`: 1 test for a 461-line `server.py`).
4. **Type check in CI** — run `mypy`/`pyright` from Concern B as a required job.

### Suggested order for the future session
1. CI skeleton (tests green on a matrix) — establishes the safety net.
2. ruff config + autofix formatting-only PR (mechanical, easy to review).
3. Type hints + type-check job.
4. Exception-handling Tier 1 → Tier 2 → Tier 3, each its own PR with regression tests.
5. Coverage gate last, once suites are expanded.

---

## Explicitly out of scope for both sessions (parking lot)
- ANN / vector index for scale (tracked in `semantic-retrieval.md` §8).
- SQLite concurrency review (WAL, `busy_timeout`) under simultaneous REST + MCP writers.
- Packaging polish: `CHANGELOG.md`, `CONTRIBUTING.md`, architecture diagram in README.
