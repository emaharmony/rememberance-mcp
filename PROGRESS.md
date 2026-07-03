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
- **Current phase:** Phase 3 — Wire semantic retrieval (single-user)
- **Current task:** (3.3) Fix the `LIMIT 500` candidate cap
- **Baseline:** 177 passed, 1 skipped (green).
- **Last session:** 2026-07-03 — completed (3.2) `_search_vector`. Now embeds the
  query via a lazy `HybridSearch._get_embed_chain()` (pipeline passes the SAME
  chain as write), cosine-ranks via `search_with_embedding(model=...)` filtering
  to rows with the matching `embedding_model` (cross-model cosine is meaningless
  + dim-safe), and falls back to keyword when there is no embedder or no matching
  vectors. Also fixed a LATENT bug: the keyword-fallback call passed `limit`
  positionally into the `tier` param (`_search_keyword(query, category, limit)`)
  → bogus `tier=` filter; now uses keyword args. `search_with_embedding` also
  now honors its `category` arg (was accepted-but-ignored). 4 tests in
  `tests/test_search_vector.py`. NOTE for 3.3: `search_with_embedding` still has
  `ORDER BY accessed_at DESC LIMIT 500` (hybrid.py) — drop/replace it so the best
  semantic match isn't cut before scoring (§7).

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

## Phase 3 — Wire semantic retrieval (single-user)
- [x] (3.1) Embed on write in `capture()` (non-blocking) — 2026-07-03
- [x] (3.2) Implement `_search_vector()` (model-matched) — 2026-07-03
- [ ] (3.3) Fix the `LIMIT 500` candidate cap
- [ ] (3.4) Fuse vector into `_search_balanced`
- [ ] (3.5) Dream-cycle backfill with true count

## Phase 4 — Chunking (all context sizes)
- [ ] (4.1) `memory_chunks` table; chunk-on-write with overlap; short memories = single chunk
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
