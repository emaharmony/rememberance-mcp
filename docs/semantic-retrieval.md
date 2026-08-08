# Design Doc: Semantic Retrieval + Multi-User Memory Server

**Status:** Proposed (revised)
**Scope:** Complete the retrieval ("R") capability with provider-agnostic vector search, designed to grow into a shared, networked, multi-user memory server.
**Supersedes:** the initial single-user 384-dim draft.

**Merge note (revall-v2):** memory-level vector search (§5.1–5.3) already shipped
independently in `recall_mcp` via `embeddings.py` + `search/hybrid.py` — that part
of this design is done, just not via the code this doc originally described.
Chunk-level retrieval (§5.4 chunking, §5.5 chunk backfill) did **not** carry over
in the initial reconciliation: it depended on modules (`chunk/chunk.py`,
`embed/embed.py`) that only existed on the pre-rename `remembrance_mcp` codebase
and were removed as part of reconciling with `recall_mcp`'s independent rewrite.
**Chunking has since been re-implemented against `recall_mcp`'s actual
architecture** rather than ported verbatim:

- `chunking.py::chunk_text()` — the splitter itself, unchanged in behavior; env
  vars renamed to `RECALL_CHUNK_*` (with `REMEMBRANCE_CHUNK_*` fallback via
  `recall_mcp.compat.get_env`, like every other Recall setting).
- Migration 12 (`memory_chunks`) — uses `recall_mcp`'s actual naming
  (`embedding_dimensions`, not the draft's `embedding_dim`) and does **not**
  duplicate `owner_id`/`scope` on chunks: `recall_mcp` already implemented the
  full formal-scope columns (§6 below) on `memories` itself, so chunks inherit
  scope through `memory_id` and cascade-delete with their parent instead.
- `MemoryStoreV2.store_chunks()` / `stale_chunk_memories()` — chunk persistence
  and backfill-candidate lookup, following the `MemoryStoreV2` (not raw SQL)
  convention the rest of the store layer uses.
- `HybridSearch.search_chunks_with_embedding()` — additive, not a replacement:
  it is merged into `_search_vector()`/`_search_balanced()` alongside the
  existing sqlite-vec-backed whole-memory path (best score per memory wins),
  so installs that never chunk see byte-identical behavior to before this
  feature existed.
- Dream-cycle `chunk_backfill` phase — gated by its own `chunking_enabled` flag
  (wired from `RECALL_CHUNKING_ENABLED`), independent of `embedding_provider`
  presence, so enabling `RECALL_EMBEDDINGS_ENABLED` alone never triggers a
  backfill for installs that haven't opted into chunking.
- Chunk-on-write in `pipeline.py::_finish_capture()` — gated by
  `RECALL_CHUNKING_ENABLED` (default **off**; see docs/configuration.md).
  Enabling it changes retrieval results (chunk hits merge into vector/balanced
  search) and queues a one-time backfill workload for pre-existing memories on
  installs that opt in later, so it is not on by default.

---

## 1. Summary

Vector-search *scoring* infrastructure already exists in the codebase (`search_with_embedding()`, cosine helpers, an `embedding BLOB` column). The missing pieces are: (a) an **embedder** to produce vectors, (b) **provider-agnostic** storage so any embedding model/dimension works, (c) **chunking** so all context sizes retrieve well, and (d) an eventual **multi-user, networked** data layer.

The target end-state (per requirements) is a **shared office memory server**: many users, multiple agent types (Ollama, Claude, Codex), reachable from anywhere, each capturing and retrieving memories. That target changes two things from the original draft: the storage engine and the schema.

**Guiding principle:** ship a clean single-user v1 now, but make every schema and interface decision forward-compatible with the multi-user target so we never require a destructive migration.

---

## 2. Two components that are often confused (settled)

- **Gate** = the "sorting brain." Classifies whether text is worth remembering and its tier. Already in the repo.
- **Embedder** = a *translator*. Turns text into a vector so semantically similar text lands nearby, even with no shared words. Does **not** sort or judge.

These are independent and both configurable. This doc is about the embedder + retrieval; the gate is unchanged.

Separately: **"condense context to be full and accurate" is summarization, not embedding.** Embeddings *find* text; they don't *shrink* it. The existing `extract` stage (produces `summary` + `key_topics`) is the condensation lever. Handled in the roadmap as its own track, not folded into vector work.

---

## 3. Goals / Non-Goals

### Goals
- Pluggable embedding provider (Ollama, OpenAI, others) via config — no hardcoded provider or dimension.
- Provider-agnostic schema: store vector dimension + model identity per row; validate against the active model.
- Chunking layer so long memories retrieve accurately (not one blurred average vector).
- Fuse vector results into existing hybrid (keyword + graph) ranking.
- Fix the `LIMIT 500` candidate-cap bug.
- Forward-compatible with a multi-user, networked deployment (per-user scoping, concurrent writers).
- All new tests run offline via a dependency-free fallback backend.

### Non-Goals (this doc; see roadmap)
- Full multi-user auth/permissions implementation (designed-for, not built in v1).
- Postgres/pgvector migration execution (specified as the scaling path, sequenced in roadmap).
- ANN indexing internals beyond choosing the direction.
- Summarization/compression improvements (separate roadmap track).

---

## 4. Current state (verified)

| Component | State | Location |
|---|---|---|
| `embedding BLOB` column | Exists, fixed-384 assumption in comments | `store/store.py` L105 |
| `store()` accepts `embedding` | Exists, defaults None | `store/store.py` L132 |
| `search_with_embedding()` | Implemented (cosine, tier boost, expiry) | `search/hybrid.py` L206 |
| serialization + cosine helpers | Implemented | `search/hybrid.py` L426–459 |
| `_search_vector()` | Stub → keyword fallback | `search/hybrid.py` L192 |
| write path embeds? | No — `capture()` omits embedding | `pipeline.py` L177 |
| dream backfill | Stub — `embeddings_refreshed: 0` | `dream/cycle.py` L357 |
| embedder | Does not exist | — |
| `extract` (condensation) | Exists: summary + key_topics | `extract/extract.py` |
| storage engine | Single-file SQLite (whole-DB write lock) | `store/store.py` |

---

## 5. Design

### 5.1 Provider-agnostic embedding (replaces fixed-384)

Because any provider must work, the schema **must not** assume 384 dims. Store dimension and model identity alongside the vector:

- `embedding BLOB` — the raw float32 vector (existing column, reused).
- `embedding_dim INTEGER` — vector length (new).
- `embedding_model TEXT` — model identifier, e.g. `ollama:all-minilm` (new).

Rules:
- On write, record dim + model with the vector.
- On search, only compare query vectors against rows with a **matching model** (different models produce incompatible spaces — cosine between them is meaningless). This makes model changes safe: old vectors remain, are simply skipped until re-embedded.
- Model change → dream-cycle backfill re-embeds rows lazily (see 5.5).

This turns "pick 384 to avoid migration" into "no dimension is hardcoded, so no migration is ever forced by a model swap."

### 5.2 New module `embed/embed.py` (mirrors gate-backend pattern)

```
BaseEmbedBackend (ABC): embed(text)->bytes ; dim:int ; model_id:str
OllamaEmbedBackend    : POST /api/embeddings ; model+host from config
OpenAIEmbedBackend    : text-embedding-3-* ; api key from config
HashEmbedBackend      : dependency-free deterministic fallback (offline tests)
EmbedFallbackChain    : ordered, first success wins ; env RECALL_EMBED_BACKENDS
  public: embed_text(text)->(bytes, dim, model_id)
```

Fallback caveat (must be in README): the hash backend produces correctly-shaped vectors so the pipeline *works* offline, but has **no real semantic quality**. Genuine retrieval needs Ollama/OpenAI. It is a correctness scaffold, not a quality one.

### 5.3 Config

- `RECALL_EMBED_BACKENDS` (default `"hash"`; opt into `"ollama,hash"` or `"openai,hash"`).
- `RECALL_EMBED_MODEL`, `RECALL_EMBED_HOST`/key as relevant.
- No `RECALL_EMBED_DIM` needed — dimension is discovered from the model and stored per row.

### 5.4 Chunking (makes "all context sizes" real)

One vector per long memory averages everything into mush. Instead:
- On write, if content exceeds a token threshold, split into overlapping chunks.
- Embed each chunk; store chunks in a `memory_chunks` table (`chunk_id`, `memory_id`, `chunk_index`, `content`, `embedding`, `embedding_dimensions`, `embedding_model` — `embedding_dimensions` to match the naming `recall_mcp` already uses on `memories`, not this draft's original `embedding_dim`).
- Search matches at the **chunk** level, then resolves to parent memories (dedup, keep best-scoring chunk per memory).
- Short memories → a single chunk (uniform code path).
- Gated by `RECALL_CHUNKING_ENABLED` (default **off** — see §5.3/docs/configuration.md): both chunk-on-write and the backfill phase are opt-in, since enabling them changes retrieval results and queues a one-time backfill for existing installs.

This is the mechanism that lets small notes and long documents coexist accurately.

### 5.5 Wiring (three call sites)
- **Write** — `pipeline.py::_finish_capture()`: chunk → embed chunks → store parent + chunks. Non-blocking: embed failure logs a warning and stores with null embedding; the memory is never lost.
- **Query** — `hybrid.py::_search_vector()`: embed query → match against chunks of the active model via `search_chunks_with_embedding()` → resolve to parents → merge additively with the existing whole-memory `search_with_embedding()` results (best score per memory wins). Falls back to keyword if no embedder/vector at all. The merge is additive by design: an install that never chunks sees `chunk_results == []` and gets byte-identical behavior to before chunking existed.
- **Backfill** — `dream/cycle.py::_phase_chunk_backfill()`: find memories with no current-model embedded chunk, (re)chunk + embed in bounded batches, report true `memories_chunked`/`chunks_written` counts. Gated by its own `chunking_enabled` flag, independent of `embedding_provider` presence.

### 5.6 Fusion
`_search_balanced()` already RRF-fuses via variadic `_rrf_fuse()`. Add vector results as an additional list. No change to the fusion algorithm.

---

## 6. Multi-user / networked (designed-for now, built later)

The shared-server target has requirements SQLite handles poorly:
- **Concurrency:** SQLite locks the whole DB per write; many simultaneous writers → `database is locked`, and the existing silent-except blocks could turn those into *lost* memories.
- **Scoping:** whose memories are whose; whether one user's memories surface in another's search; shared vs. private scopes.
- **Reach:** multiple machines/agents hitting one store over a network.

**Direction:** migrate the store to **Postgres + pgvector** for the multi-user phase. pgvector gives real concurrency, network access, and native ANN indexing in one move.

**Forward-compatibility we bake in now (so v1 doesn't block the migration):**
- `memories` already carries full formal-scope columns (`user_id`, `workspace_id`, `project_id`, `repository_id`, `task_id`, `session_id` — migration 6 `task_session_continuity`), superseding this draft's originally-proposed `owner_id`/`scope` pair. `memory_chunks` does **not** duplicate scope columns: chunks cascade-delete with their parent memory and inherit its scope through `memory_id`, so a chunk is never orphaned from — or scoped differently than — the memory it came from.
- Keep all storage access behind the existing store interface so the engine can be swapped without touching pipeline/search/API.
- Avoid SQLite-only SQL idioms in the store layer where a portable form exists.

This means single-user v1 ships clean, and the Postgres cutover is an adapter swap plus a data copy — not a redesign.

---

## 7. The `LIMIT 500` bug (in-scope fix)

`search_with_embedding()` caps candidates via `ORDER BY accessed_at DESC LIMIT 500` **before** similarity — so the best semantic match is dropped if not recently accessed. Fix now: scan all non-expired, model-matching embedded rows (acceptable for low tens of thousands). Long term this dissolves under pgvector's ANN index (§6). If a cap must remain, make it large, configurable, and relevance-neutral (not `accessed_at`).

---

## 8. Testing (offline via a deterministic hash-based `EmbeddingProvider`)
1. Round-trip: embed → store → `_search_vector` returns closest seeded memory (`tests/test_chunk_on_write.py`).
2. Provider-agnostic: two models with different dims coexist; search only compares within the active model (`tests/test_hybrid_search.py`, `tests/test_chunk_search.py`).
3. Chunking: a long memory findable by a query matching only one of its chunks; parent resolution dedups (`tests/test_chunk_search.py`).
4. Fusion: vector-only/chunk-only-findable memory surfaces via `_search_balanced` (`tests/test_chunk_search.py`).
5. Write resilience: embed/chunk failure → memory still stored, no exception escapes (`tests/test_chunk_on_write.py`).
6. Backfill: dream chunk_backfill (re)chunks null/wrong-model rows, accurate count, gated independently of `embedding_provider` (`tests/test_dream_chunk_backfill.py`).
7. No-cap regression: >500 memories, target accessed last, still retrievable (`tests/test_production_hardening.py`).
8. Forward-compat: formal-scope columns present and default correctly in single-user mode (`tests/test_task_session_continuity.py`); chunk cascade-delete verified in `tests/test_schema_migrations.py`.
9. Migration: clean `11 -> 12`, idempotent `12 -> 12`, existing rows preserved (`tests/test_schema_migrations.py`).

---

## 9. Open questions
- Chunk size / overlap defaults, and the token threshold below which a memory stays single-chunk.
- Default `scope` for office deployment: private-by-default with explicit sharing, or shared-by-default within a team?
- Postgres cutover trigger: user count, memory count, or first real concurrent-write pain?
