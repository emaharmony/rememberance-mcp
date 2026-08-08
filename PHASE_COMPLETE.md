# ALL PHASES COMPLETE — Phases 0–4

**Merge note (revall-v2):** this describes work done on the pre-rename
`remembrance_mcp` codebase. Reconciling with `recall_mcp`'s independent rewrite
superseded most of it: `recall_mcp` already shipped its own memory-level vector
search (Phases 1–3 territory) via `embeddings.py` + `search/hybrid.py`, so its
version won every merge conflict, and the Phase 0 package reorg (`gate/`,
`server/`) did not survive. **Phase 4 (chunking) has since been re-implemented**
against `recall_mcp` — the original depended on modules (`chunk/chunk.py`,
`embed/embed.py`) that were removed as part of the reconciliation, so it was
rebuilt on `recall_mcp`'s own abstractions: `chunking.py` (`chunk_text`),
migration 12 (`memory_chunks`), `MemoryStoreV2.store_chunks`/
`stale_chunk_memories`, `HybridSearch.search_chunks_with_embedding` (merged
additively into `_search_vector`/`_search_balanced`), chunk-on-write in
`pipeline.py`, and a `chunk_backfill` dream phase — all gated behind
`RECALL_CHUNKING_ENABLED` (default off; see CHANGELOG.md and
docs/semantic-retrieval.md). The rest of this file is kept as a historical
record of that loop run.

The autonomous loop finished **all of Phases 0–4** (semantic-retrieval roadmap)
per the run-all-phases override. **Stopped for human review.** Nothing is pushed;
everything is on the `autonomous-loop` branch.

## Final state
- Tests: **212 passed, 1 skipped** (green).
- Coverage: **68.89%** statement (was 63.74% at end of Phase 0; gate floor 60%).
- One **known flaky test** remains (see bottom) — not a regression.

## What shipped, by phase

### Phase 0 — Safety net
- CI matrix `.github/workflows/ci.yml` (Py 3.10/3.11/3.12 × {minimal, all}).
- `ruff` config + `.pre-commit-config.yaml`; one mechanical format pass (47 files).
- `pytest-cov` fail-under gate (floor 60%, invoked in CI only).
- Commits: `c899507`, `dbc63f8`, `6a29a38`, `e2b260f` (+ `1bb4c33`, `155e1b0`, `388d122`, `ec7850e`).

### (housekeeping) Package reorg — `6eda7bd`
- Grouped loose modules into `gate/` (backends, registry) and `server/` (mcp, serve, nats_sub). Public API + `python -m` entry preserved. *(User-requested, mid-run.)*

### Phase 1 — Embedder module (isolated) — `12bc8bd`
- `embed/embed.py`: `BaseEmbedBackend`, `HashEmbedBackend` (offline scaffold),
  `OllamaEmbedBackend`, `OpenAIEmbedBackend`, `EmbedFallbackChain`,
  `build_embed_chain`, `embed_text()->(bytes,dim,model_id)`. No hardcoded dim;
  float32 bytes match `search/hybrid.py`.

### Phase 2 — Provider-agnostic schema — `8806994`
- Additive `_migrate_v2` columns on `memories`: `embedding_dim`, `embedding_model`
  (§5.1) + `owner_id`, `scope` (§6 multi-user forward-compat, unused in v1).

### Phase 3 — Wire semantic retrieval — `ffab7f0`, `2955455`, `fdfd516`, `92c6d4e`, `40be4ed`
- 3.1 embed-on-write in `capture()` (non-blocking).
- 3.2 model-matched `_search_vector` (+ fixed two latent bugs).
- 3.3 removed the `LIMIT 500` candidate cap (§7) with a regression test.
- 3.4 fused vector into `_search_balanced` via the existing RRF.
- 3.5 dream `embed_stale` backfill with a true count.

### Phase 4 — Chunking — `ae3f3ca`, `29e1f18`, `c7ed2c9`, `5b89c4f`
- 4.1a `chunk/chunk.py` `chunk_text()` — overlapping word-boundary chunks,
  single-chunk collapse for short memories.
- 4.1b `memory_chunks` table + non-blocking chunk-on-write in `capture()`.
- 4.2 chunk-level vector search resolved to parents (best chunk per memory).
- 4.3 dream `chunk_backfill` phase for legacy/wrong-model memories.

## Review checklist for the human
1. **CI has never actually run on GitHub** — validated locally only. Push the
   branch and confirm all 6 matrix jobs go green.
2. **The 47-file mechanical reformat** (`6a29a38`) is large but behavior-preserving.
3. **Semantic quality is untested end-to-end** — the whole run used the offline
   **hash** embedding backend (deterministic, NO semantic meaning). It proves the
   *plumbing* (write→chunk→embed→store→chunk-search→fuse→backfill), not retrieval
   *quality*. Real quality needs a run with Ollama/OpenAI (`RECALL_EMBED_BACKENDS=ollama,hash`).

## Decisions I made provisionally (please confirm or change)
- **Chunk sizing** (roadmap defers this to you): I did NOT hard-block; I used
  conventional, **env-configurable** defaults — 512 target / 64 overlap / 512
  single-chunk-threshold tokens (token≈4 chars), via
  `RECALL_CHUNK_{TARGET,OVERLAP,SINGLE_THRESHOLD}_TOKENS`. Change freely.
- **Coverage floor 60%** (roadmap said ~80%): set as a ratchet floor since actual
  was 64%; now 69%. Raise toward 80% when ready.
- **Redundant embed on short capture**: a short memory is embedded once for the
  memory row and once for its single chunk. Minor; reused where the chunk equals
  the whole text. Could be optimized if real-backend cost matters.

## Parked / not done (out of Phases 0–4 scope)
- **~90 non-autofixable ruff lint findings** remain (E501, and a repeated
  `from __future__ import annotations`-before-docstring E402 in several modules).
  NOT wired into CI. A dedicated lint-cleanup task would clear them.
- **Known flake:** `test_prism_compat.py::test_v1_context_build_returns_markdown`
  fails intermittently under full-suite load (FTS5 external-content index
  visibility race). Passes 5/5 isolated and on full re-run. Documented in
  PROGRESS.md "Known flakes"; needs a reliable repro before touching code.
- Phases 5+ (hardening, Postgres/pgvector multi-user, networked server,
  condensation) — explicitly out of the loop's scope; need human review first.

## To relaunch
Delete this file. There is no Phase 5 in the loop's scope, so the loop would
write a "Phases 0–4 complete; Phases 5+ need human review" block and stop anyway.
