# PROGRESS

Shared state for the autonomous loop (see `docs/autonomous-loop.md`).
Each session: read this + git, do exactly one task, keep tests green, commit,
update this file. Only Phases 0–4 are in scope for the loop.

## Status line
- **Current phase:** Phase 0 — Safety net
- **Current task:** (0.2b) mechanical `ruff format` + `ruff check --fix` pass
- **Baseline:** 141 passed, 1 skipped (green).
- **Last session:** 2026-07-02 — completed (0.2a): added ruff config to
  `pyproject.toml` (line-length 100, lint select E/F/W/I, isort first-party
  `remembrance_mcp`) + `.pre-commit-config.yaml` (ruff + ruff-format, pinned
  v0.15.20) + `ruff>=0.15.0` in the dev/all extras. Config-only, no code
  reformatted. ruff 0.15.20 installed in `.venv`. Suite green.
  **Preview for 0.2b:** `ruff check .` → 285 findings (150 autofixable);
  `ruff format` → 45 of 47 files would reformat. That is the intended large
  mechanical diff — keep it format/autofix ONLY, its own commit, no logic edits.

## Environment (READ THIS FIRST)
- **Tests MUST run via the venv:** `.venv/Scripts/python.exe -m pytest -q`.
  Bare `python` on this machine is Python 3.14 core with NO pytest installed.
- Repo work happens on the `autonomous-loop` branch.

## Design docs
- `docs/roadmap.md` — phase sequence + dependency graph
- `docs/semantic-retrieval.md` — feature design (Phases 1–4)
- `docs/deferred-hardening.md` — hygiene/exceptions (Phase 0, Phase 5)

---

## Phase 0 — Safety net *(active)*
- [x] (0.1) CI: GitHub Actions matrix (Py 3.10–3.12, minimal + `[all]` extras) running the suite — `.github/workflows/ci.yml` (2026-07-02)
- [x] (0.2a) `ruff` config in `pyproject.toml` + `.pre-commit-config.yaml` (2026-07-02)
- [ ] (0.2b) mechanical `ruff format` + `ruff check --fix` pass (its own commit; no logic edits)
- [ ] (0.3) `pytest-cov` with a fail-under gate

## Phase 1 — Embedder module (isolated, no wiring)
- [ ] (1.1) `embed/embed.py`: `BaseEmbedBackend`, `OllamaEmbedBackend`, `OpenAIEmbedBackend`, `HashEmbedBackend`, `EmbedFallbackChain`
- [ ] (1.2) Config env vars; `embed_text()` returns `(bytes, dim, model_id)`
- [ ] (1.3) Offline unit tests via hash backend

## Phase 2 — Provider-agnostic schema
- [ ] (2.1) Add `embedding_dim`, `embedding_model` to memories
- [ ] (2.2) Add `owner_id` (nullable), `scope` (default `private`) — unused in v1, structurally present
- [ ] (2.3) Additive-only migration (no data loss on existing stores)

## Phase 3 — Wire semantic retrieval (single-user)
- [ ] (3.1) Embed on write in `capture()` (non-blocking)
- [ ] (3.2) Implement `_search_vector()` (model-matched)
- [ ] (3.3) Fix the `LIMIT 500` candidate cap
- [ ] (3.4) Fuse vector into `_search_balanced`
- [ ] (3.5) Dream-cycle backfill with true count

## Phase 4 — Chunking (all context sizes)
- [ ] (4.1) `memory_chunks` table; chunk-on-write with overlap; short memories = single chunk
- [ ] (4.2) Search at chunk level → resolve to parent memories (dedup, best chunk wins)
- [ ] (4.3) Backfill chunks for existing memories in the dream cycle

---

## Phase gate
On completing the LAST task of a phase: write `PHASE_COMPLETE.md` naming the
phase + what shipped, then STOP for human review.

## Escalation
If blocked on a deferred product decision (chunk sizing beyond doc defaults,
privacy scope defaults, Postgres cutover) or 3 failed approaches: write
`BLOCKED.md` and stop. Open decisions are listed in `docs/roadmap.md`.

## Known flakes (do NOT treat as a regression)
- `tests/test_prism_compat.py::test_v1_context_build_returns_markdown` fails
  intermittently under full-suite load (~1-in-several runs; 0/5 in isolation).
  Root cause hypothesis: `build_context` → `_search_balanced` retrieves via
  FTS5 keyword match only (`search/hybrid.py`: `vec_results` is a placeholder),
  and the FTS5 external-content index occasionally isn't populated/visible for
  the just-captured row when `/v1/context/build` runs immediately after
  `/capture`. This is the "FTS5 index silently returning nothing" mode the test
  file docstring already calls out. Note the sibling `test_capture_then_search_finds_it`
  (keyword mode) is the more direct guard for the same path.
  A proper fix (make FTS population deterministic/synchronous on capture, or the
  test wait for index visibility) needs a reliable local repro before touching
  code — do NOT weaken the assertion. When CI exists (task 0.1), a rerun-on-flake
  or repeat-run job will surface the true failure rate.
