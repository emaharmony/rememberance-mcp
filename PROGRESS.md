# PROGRESS

Shared state for the autonomous loop (see `docs/autonomous-loop.md`).
Each session: read this + git, do exactly one task, keep tests green, commit,
update this file. Only Phases 0–4 are in scope for the loop.

## Status line
- **Current phase:** Phase 0 — Safety net
- **Current task:** (0.1) CI GitHub Actions matrix
- **Baseline:** 141 passed, 1 skipped (green).
- **Last session:** 2026-07-02 — bootstrapped PROGRESS.md from `docs/roadmap.md`
  Phases 0–4 and verified the baseline test suite is green.

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
- [ ] (0.1) CI: GitHub Actions matrix (Py 3.10–3.12, minimal + `[all]` extras) running the suite
- [ ] (0.2) `ruff` lint+format config + pre-commit; one mechanical formatting-only PR
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
