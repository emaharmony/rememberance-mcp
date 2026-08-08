# Roadmap: remembrance-mcp → Shared Semantic Memory Server

Sequenced plan from the current single-user keyword store to a networked, multi-user, semantically-retrieving office memory server. Ordered so **every phase ships something working** and nothing forces a destructive rewrite later.

Companion docs: `semantic-retrieval.md` (feature design), `deferred-hardening.md` (exception/type/tooling cleanup).

---

## Guiding principles
- Ship working software at every phase; fallbacks preserve current behavior.
- No hardcoded provider or dimension — configurable from day one.
- Bake multi-user structure into the schema early (nullable/unused in v1) so the Postgres cutover is an adapter swap, not a redesign.
- Keep feature work and hygiene work in separate PRs for reviewability.

---

## Phase 0 — Safety net (do first, enables everything)
*From `deferred-hardening.md` Concern C.*
- CI: GitHub Actions matrix (Py 3.10–3.12, minimal + `[all]` extras) running the suite.
- `ruff` lint+format config + pre-commit; one mechanical formatting-only PR.
- `pytest-cov` with a fail-under gate.
**Why first:** you can't safely refactor storage or wire embeddings without a green baseline proving you didn't break anything.

## Phase 1 — Embedder module (isolated, no wiring)
*`semantic-retrieval.md` §5.2.*
- `embed/embed.py`: `BaseEmbedBackend`, `OllamaEmbedBackend`, `OpenAIEmbedBackend`, `HashEmbedBackend`, `EmbedFallbackChain`.
- Config env vars; `embed_text()` returns `(bytes, dim, model_id)`.
- Offline unit tests via hash backend.
**Independently shippable; touches nothing else.**

## Phase 2 — Provider-agnostic schema
*§5.1 + §6 forward-compat.*
- Add `embedding_dim`, `embedding_model` to memories.
- Add `owner_id` (nullable), `scope` (default `private`) — unused in v1, structurally present.
- Migration that's additive-only (no data loss on existing stores).
**Ships invisibly; sets up every later phase.**

## Phase 3 — Wire semantic retrieval (single-user)
*§5.5, §5.6, §7.*
- Embed on write in `capture()` (non-blocking).
- Implement `_search_vector()` (model-matched).
- Fix the `LIMIT 500` candidate cap.
- Fuse vector into `_search_balanced`.
- Dream-cycle backfill with true count.
**First phase where semantic search actually works end-to-end.**

## Phase 4 — Chunking (all context sizes)
*§5.4.*
- `memory_chunks` table; chunk-on-write with overlap; short memories = single chunk.
- Search at chunk level → resolve to parent memories (dedup, best chunk wins).
- Backfill chunks for existing memories in the dream cycle.
**Makes long-context memories retrieve accurately, not as blurred averages.**

## Phase 5 — Hardening pass (Tiers 1–3)
*`deferred-hardening.md` Concern A + B.*
- Narrow the 30 broad exception blocks, Tier 1 (data-integrity) first, each with a regression test.
- Complete type hints on `server.py`, `serve.py`, `__main__.py`; add `mypy`/`pyright` to CI.
**Do before multi-user:** silent exception-swallowing becomes *data loss* under concurrent writers, so this must land before the network exposes it.

## Phase 6 — Multi-user data layer (Postgres + pgvector)
*§6.*
- Implement a Postgres store adapter behind the existing store interface.
- pgvector for vectors (native concurrency + ANN indexing; dissolves the §7 scan concern).
- Activate `owner_id` + `scope`: private-by-default, explicit sharing (pending §9 decision).
- Data-copy migration path from SQLite.
**The cutover the earlier phases were built to make painless.**

## Phase 7 — Networked server + multi-agent access
- Harden the REST/MCP surface for remote, multi-user access (authn/authz, per-user scoping enforced in queries).
- Verify Ollama, Claude, and Codex clients all reach the shared server.
- Concurrency validation: simultaneous writers, no lost memories, no lock errors.

## Phase 8 — Condensation track (parallel, independent)
*Separate from retrieval; can run alongside Phases 3–7.*
- Improve the `extract` stage's summarization for accurate, compact memory content.
- Optional: hierarchical summaries (per-memory → per-topic) to keep injected context full but small.
**This is the "condense context to be full and accurate" wish — a summarization capability, not an embedding one.**

---

## Dependency graph (what blocks what)
- Phase 0 blocks everything (safety net).
- Phases 1 → 2 → 3 → 4 are strictly ordered (retrieval core).
- Phase 5 should precede Phase 6/7 (concurrency turns silent errors into data loss).
- Phase 6 depends on Phase 2's forward-compat columns.
- Phase 8 is parallel and depends only on Phase 0.

## Session mapping (suggested)
- **Session A (this track):** Phase 0 + Phase 1.
- **Session B:** Phase 2 + Phase 3.
- **Session C:** Phase 4.
- **Session D:** Phase 5 (hardening).
- **Session E+:** Phases 6–7 (multi-user server).
- **Anytime:** Phase 8 (condensation).

## Open decisions carried forward
- Chunk size/overlap + single-chunk threshold (Phase 4).
- Default scope for office use: private-with-sharing vs. team-shared (Phase 6).
- Postgres cutover trigger: user count, memory count, or first concurrent-write pain (Phase 6).
