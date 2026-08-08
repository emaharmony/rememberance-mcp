# PROGRESS

**Merge note (revall-v2):** this file records the autonomous loop's work on the
pre-rename `remembrance_mcp` codebase (Phases 0-4). That codebase has since been
reconciled with `recall_mcp`'s independent rewrite: `recall_mcp` already had its
own (more mature) memory-level vector search, so it won every conflict, and the
package reorg described below (`gate/`, `server/`) did not survive. The
chunking work (Phase 4) also did not carry over as-is — it depended on modules
only present on the old codebase — and is pending re-implementation against
`recall_mcp`, with a reference implementation preserved out-of-tree for that
follow-up. The rest of this file is kept as a historical record of that loop run.

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
- **Current phase:** ALL PHASES 0–4 COMPLETE (2026-07-03) — see PHASE_COMPLETE.md
- **Current task:** DONE — loop finished. Awaiting human review.
- **Baseline:** 212 passed, 1 skipped (green).
- **Last session:** 2026-07-03 — completed (4.3) dream chunk backfill + **Phase 4
  DONE + ALL PHASES DONE**. Added `chunk_backfill` phase to the dream cycle
  (`_phase_chunk_backfill`): finds memories lacking a current-model embedded
  chunk (legacy/no-chunks or post-model-swap), re-runs chunk_text → embed →
  store_chunks, bounded per run, returns memories_chunked/chunks_written + dry-run.
  5 tests `tests/test_dream_chunk_backfill.py`. Wrote PHASE_COMPLETE.md and stopped
  per RUN MODE.

## Environment (READ THIS FIRST)
- **Tests MUST run via the venv:** `.venv/Scripts/python.exe -m pytest -q`.
  Bare `python` on this machine is Python 3.14 core with NO pytest installed.
- Repo work happens on the `autonomous-loop` branch.
- **Package layout (reorganized 2026-07-02, superseded by the revall-v2 merge):**
  at the time, gate backends lived in `gate/backends.py` + `gate/registry.py`;
  runtime entry points lived in `server/` (`server/mcp.py` = MCP server,
  `server/serve.py` = REST launcher, `server/nats_sub.py`). This did not survive
  reconciling with `recall_mcp`, which independently kept a flat layout
  (`gate_backends.py`, `registry.py`, `serve.py`, `server.py`, `nats_sub.py` all
  at the package root). `python -m remembrance_mcp` (MCP stdio) still works as a
  deprecated back-compat shim; the real implementation and REST entry point now
  live under `recall_mcp` (`python -m recall_mcp.serve`).

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

## Phase 4 — Chunking (all context sizes) *(COMPLETE 2026-07-03)*
- [x] (4.1a) chunking primitive `chunk/chunk.py` `chunk_text()` (env-configurable) — 2026-07-03
- [x] (4.1b) `memory_chunks` table + chunk-on-write wiring — 2026-07-03
- [x] (4.2) Search at chunk level → resolve to parent memories (dedup, best chunk wins) — 2026-07-03
- [x] (4.3) Backfill chunks for existing memories in the dream cycle — 2026-07-03

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
