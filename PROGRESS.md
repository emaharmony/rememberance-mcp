# PROGRESS

Shared state for the autonomous loop (see `docs/autonomous-loop.md`).
Each session: read this + git, do exactly one task, keep tests green, commit,
update this file. Only Phases 0–4 are in scope for the loop.

## RUN MODE (user override, 2026-07-02)
Run **all** phases (0–4) end to end WITHOUT stopping at per-phase gates. Do NOT
write `PHASE_COMPLETE.md` at each phase boundary — only write it ONCE, after the
LAST task of Phase 4 is done and green. Everything else in the protocol still
holds: one task per session, tests are the arbiter, `BLOCKED.md` halts on a
deferred product decision or 3 failed approaches. User reviews all work later.

## Status line
- **Current phase:** Phase 4 — Chunking (all context sizes)
- **Current task:** (4.1b) `memory_chunks` table + chunk-on-write wiring
- **Baseline:** 197 passed, 1 skipped (green).
- **Last session:** 2026-07-03 — completed (4.1a) the chunking primitive.
  `chunk/chunk.py` `chunk_text()`: empty→[], short(≤threshold)→[text] single
  chunk, else overlapping word-boundary chunks (full word coverage, order
  preserved, forward-progress guaranteed even with degenerate overlap≥target).
  Sizing is CONVENTIONAL + env-configurable, NOT a final product decision —
  defaults 512 target / 64 overlap / 512 single-threshold tokens (token≈4 chars),
  overridable via `REMEMBRANCE_CHUNK_{TARGET,OVERLAP,SINGLE_THRESHOLD}_TOKENS`.
  12 tests in `tests/test_chunk.py`. Chose configurable-defaults over BLOCKED.md
  per RUN MODE ("complete all phases") — reversible, flagged for human review.
  NOTE for 4.1b: add a `memory_chunks` table (chunk_id, memory_id, chunk_index,
  content, embedding, embedding_dim, embedding_model, + owner_id/scope §6) via
  `_migrate_v2`; on capture() write parent, then chunk_text(content), embed each
  chunk, insert chunks (short memory → 1 chunk, uniform path). Non-blocking.

## Environment (READ THIS FIRST)
- **Tests MUST run via the venv:** `.venv/Scripts/python.exe -m pytest -q`.
  Bare `python` on this machine is Python 3.14 core with NO pytest installed.
- Repo work happens on the `autonomous-loop` branch.
- **Package layout (reorganized 2026-07-02):** gate backends live in
  `gate/backends.py` + `gate/registry.py`; runtime entry points live in
  `server/` (`server/mcp.py` = MCP server, `server/serve.py` = REST launcher,
  `server/nats_sub.py`). `config.py`/`pipeline.py` stay at the package root.
  `python -m remembrance_mcp` (MCP stdio) still works; REST is now
  `python -m remembrance_mcp.server.serve`. Top-level `remembrance_mcp` re-exports
  are unchanged (public API stable).

## Design docs
- `docs/roadmap.md` — phase sequence + dependency graph
- `docs/semantic-retrieval.md` — feature design (Phases 1–4)
- `docs/deferred-hardening.md` — hygiene/exceptions (Phase 0, Phase 5)

---

## Phase 0 — Safety net *(COMPLETE 2026-07-02)*
CI matrix (`.github/workflows/ci.yml`), ruff config + pre-commit, mechanical
format pass, pytest-cov fail-under gate (floor 60%). Details in git history.

## Phase 1 — Embedder module *(COMPLETE 2026-07-02)*
`embed/embed.py` (5 backends + `EmbedFallbackChain` + `build_embed_chain` +
`embed_text()`), `embed/__init__.py`, `tests/test_embed.py` (21 offline tests).
Isolated — wired in nowhere yet (that is Phase 3). Details in git history.

## Phase 2 — Provider-agnostic schema *(COMPLETE 2026-07-03)*
`embedding_dim`, `embedding_model`, `owner_id`, `scope` added to `memories` via
the additive idempotent `_migrate_v2`; `tests/test_schema_migration.py`. Details
in git history.

## Phase 3 — Wire semantic retrieval (single-user) *(COMPLETE 2026-07-03)*
Embed-on-write (non-blocking), model-matched `_search_vector`, LIMIT-500 fix,
vector fusion into `_search_balanced`, dream backfill with true count. Semantic
search is live end-to-end. Details in git history + `tests/test_embed_on_write`,
`test_search_vector`, `test_search_balanced`, `test_dream_backfill`.

## Phase 4 — Chunking (all context sizes)
- [x] (4.1a) chunking primitive `chunk/chunk.py` `chunk_text()` (env-configurable) — 2026-07-03
- [ ] (4.1b) `memory_chunks` table + chunk-on-write wiring (short memory = single chunk)
- [ ] (4.2) Search at chunk level → resolve to parent memories (dedup, best chunk wins)
- [ ] (4.3) Backfill chunks for existing memories in the dream cycle

---

## Phase gate (SUPERSEDED by RUN MODE above)
Per the user override, do NOT stop or write `PHASE_COMPLETE.md` at intermediate
phase boundaries. Only after the LAST task of **Phase 4** is done and green:
write `PHASE_COMPLETE.md` summarizing all of Phases 0–4, then stop.

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
