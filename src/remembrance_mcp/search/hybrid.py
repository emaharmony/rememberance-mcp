"""
Hybrid Search — FTS5 + Vector + Graph + RRF Fusion

PATTERN: Multi-Strategy Retrieval with Fusion
================================================

gbrain proved that single-strategy retrieval is insufficient:
- Vector-only search: 18% P@5
- Keyword-only search: ~25% P@5
- Vector + Keyword (RRF): ~35% P@5
- Full stack (+ graph augmentation): 49% P@5

The +31 P@5 lift from graph augmentation is the key insight:
connections between entities surface results that neither
vector similarity nor keyword matching can find.

V2 ARCHITECTURE:
1. Vector search: cosine similarity on embedding BLOBs in SQLite
2. FTS5 search: SQLite full-text search with BM25 ranking
3. Source-tier boost: PERSIST > ACTIVE > COLD
4. RRF fusion: reciprocal rank fusion merges all strategies
5. Graph augment: expand results by following entity edges
"""

import logging
import math
import sqlite3
import struct
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from remembrance_mcp.store.edges import EntityStore

logger = logging.getLogger(__name__)


@contextmanager
def _connect(db_path: Path):
    """Open a SQLite connection that commits/rolls back and closes on Windows."""
    conn = sqlite3.connect(str(db_path))
    try:
        with conn:
            yield conn
    finally:
        conn.close()


# ── Tier Boost Multipliers ─────────────────────────────────────

TIER_BOOST = {
    "persist": 1.5,
    "active": 1.0,
    "cold": 0.5,
    "skip": 0.1,
}

# ── RRF Constant ───────────────────────────────────────────────
# Standard RRF constant (k=60 is the literature default)
RRF_K = 60


@dataclass
class SearchResult:
    """A single search result with score and metadata."""

    id: str
    content: str
    compiled_truth: str
    summary: str
    category: str
    tier: str
    score: float
    sources: list[str]  # which strategies found this result
    entities: list[str]  # entity IDs linked to this result


class HybridSearch:
    """
    Hybrid search combining FTS5, vector similarity, and graph augmentation.

    Usage:
        search = HybridSearch(db_path, entity_store)
        results = search.search("What did we decide about Prism?")
    """

    def __init__(self, db_path: Path, entity_store: EntityStore, embed_chain=None):
        self.db_path = db_path
        self.entity_store = entity_store
        # Embedder for query-time vectors. Lazy so keyword-only use pays nothing;
        # defaults to the env-configured chain (same one capture() writes with).
        self._embed_chain = embed_chain

    def _get_embed_chain(self):
        if self._embed_chain is None:
            from remembrance_mcp.embed.embed import build_embed_chain

            self._embed_chain = build_embed_chain()
        return self._embed_chain

    def search(
        self,
        query: str,
        mode: str = "balanced",
        category: Optional[str] = None,
        tier: Optional[str] = None,
        limit: int = 10,
    ) -> list[dict]:
        """
        Search memories using hybrid retrieval.

        Modes:
        - "keyword": FTS5 only (fastest, cheapest)
        - "vector": Cosine similarity only
        - "balanced": FTS5 + vector + tier boost + graph (DEFAULT)
        - "deep": balanced + LLM query expansion (future)

        Returns list of result dicts sorted by fused score.
        """
        if mode == "keyword":
            return self._search_keyword(query, category, tier, limit)
        elif mode == "vector":
            return self._search_vector(query, category, limit)
        elif mode == "balanced":
            return self._search_balanced(query, category, tier, limit)
        elif mode == "deep":
            # Future: add LLM query expansion
            return self._search_balanced(query, category, tier, limit)
        else:
            return self._search_balanced(query, category, tier, limit)

    def _search_keyword(
        self,
        query: str,
        category: Optional[str] = None,
        tier: Optional[str] = None,
        limit: int = 10,
    ) -> list[dict]:
        """FTS5 full-text search only."""
        results = []
        with _connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            try:
                # Escape FTS5 special chars: hyphens become spaces, quotes escaped
                safe_query = query.replace("-", " ").replace('"', '""')
                sql = """
                    SELECT m.*, fts.rank as fts_rank
                    FROM memories m
                    JOIN memories_fts fts ON m.rowid = fts.rowid
                    WHERE memories_fts MATCH ?
                """
                params = [safe_query]

                if category:
                    sql += " AND m.category = ?"
                    params.append(category)
                if tier:
                    sql += " AND m.tier = ?"
                    params.append(tier)

                now = time.time()
                sql += " AND (m.expires_at IS NULL OR m.expires_at > ?)"
                params.append(now)

                sql += " ORDER BY fts.rank LIMIT ?"
                params.append(limit)

                rows = conn.execute(sql, params).fetchall()
                for r in rows:
                    d = dict(r)
                    d["score"] = 1.0 / (
                        1.0 + abs(d.pop("fts_rank", 0))
                    )  # FTS rank is negative (lower = better)
                    d["sources"] = ["fts5"]
                    results.append(d)
            except Exception as e:
                if isinstance(e, sqlite3.OperationalError):
                    logger.debug(f"FTS5 search failed (OperationalError): {e}, using LIKE fallback")
                else:
                    logger.warning(f"FTS5 search failed: {e}, using LIKE fallback")
                # LIKE fallback
                sql = "SELECT * FROM memories WHERE 1=1"
                params = []

                if query:
                    sql += " AND (content LIKE ? OR compiled_truth LIKE ?)"
                    params.extend([f"%{query}%", f"%{query}%"])

                if category:
                    sql += " AND category = ?"
                    params.append(category)
                if tier:
                    sql += " AND tier = ?"
                    params.append(tier)

                now = time.time()
                sql += " AND (expires_at IS NULL OR expires_at > ?)"
                params.append(now)

                sql += " ORDER BY accessed_at DESC LIMIT ?"
                params.append(limit)

                rows = conn.execute(sql, params).fetchall()
                for r in rows:
                    d = dict(r)
                    d["score"] = 0.5  # Lower than FTS5 matches
                    d["sources"] = ["like"]
                    results.append(d)

        return results

    def _search_vector(self, query: str, category: Optional[str], limit: int) -> list[dict]:
        """
        Vector similarity search (semantic-retrieval.md §5.5 "Query").

        Embeds the query with the configured chain, then cosine-ranks against
        rows embedded by the SAME model (cross-model cosine is meaningless).
        Falls back to keyword search when there is no embedder or no matching
        vectors yet — so search is always useful, never empty for lack of vectors.
        """
        try:
            query_bytes, _dim, model_id = self._get_embed_chain().embed_text(query)
        except Exception as e:
            logger.warning(f"Query embedding failed, using keyword search: {e}")
            return self._search_keyword(query, category=category, limit=limit)

        results = self.search_with_embedding(
            query_bytes, category=category, limit=limit, model=model_id
        )
        if not results:
            # No rows embedded with this model yet (e.g. fresh store, or a model
            # swap pending dream-cycle backfill) — keyword keeps search working.
            return self._search_keyword(query, category=category, limit=limit)
        return results

    def search_with_embedding(
        self,
        query_embedding: bytes,
        category: Optional[str] = None,
        limit: int = 10,
        model: Optional[str] = None,
    ) -> list[dict]:
        """
        Search using a pre-computed embedding vector.

        Args:
            query_embedding: The query vector as bytes (float32 array)
            category: Optional category filter
            limit: Maximum results
            model: Optional embedding-model filter. Only rows produced by this
                   model are compared — different models live in incompatible
                   vector spaces (and may differ in dimension), so cosine across
                   them is meaningless.

        Returns:
            List of results sorted by cosine similarity
        """
        query_vec = self._bytes_to_vector(query_embedding)
        if not query_vec:
            return []

        candidates = []
        with _connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            now = time.time()

            sql = """
                SELECT id, content, compiled_truth, summary, category, tier,
                       embedding, key_topics, source
                FROM memories
                WHERE embedding IS NOT NULL
                AND (expires_at IS NULL OR expires_at > ?)
            """
            params: list = [now]
            if model is not None:
                sql += " AND embedding_model = ?"
                params.append(model)
            if category is not None:
                sql += " AND category = ?"
                params.append(category)
            sql += " ORDER BY accessed_at DESC LIMIT 500"

            rows = conn.execute(sql, params).fetchall()

            for r in rows:
                if not r["embedding"]:
                    continue
                mem_vec = self._bytes_to_vector(r["embedding"])
                if not mem_vec:
                    continue

                similarity = self._cosine_similarity(query_vec, mem_vec)
                tier_boost = TIER_BOOST.get(r["tier"], 1.0)
                boosted_score = similarity * tier_boost

                candidates.append(
                    {
                        "id": r["id"],
                        "content": r["content"],
                        "compiled_truth": r["compiled_truth"] or "",
                        "summary": r["summary"] or "",
                        "category": r["category"],
                        "tier": r["tier"],
                        "score": boosted_score,
                        "sources": ["vector"],
                    }
                )

        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[:limit]

    def _search_balanced(
        self,
        query: str,
        category: Optional[str] = None,
        tier: Optional[str] = None,
        limit: int = 10,
    ) -> list[dict]:
        """
        Balanced hybrid search: FTS5 + vector + tier boost + graph + RRF.

        Pipeline:
        1. Run FTS5 search → get top 30
        2. Run vector search → get top 30 (if embeddings available)
        3. Apply tier boost to both result sets
        4. RRF fusion to merge rankings
        5. Graph augmentation: expand results by following entity edges
        6. Deduplicate and return top N
        """
        # Step 1: FTS5 search
        fts_results = self._search_keyword(query, category, tier, limit=30)

        # Step 2: Vector search (placeholder — needs embedding generation)
        # For V2.3, vector results come from search_with_embedding if caller has embedding
        vec_results = []  # Will be populated when embedding generation is wired

        # Step 3+4: RRF fusion
        fused = self._rrf_fuse(fts_results, vec_results)

        # Step 5: Graph augmentation
        if self.entity_store:
            augmented = self._graph_augment(query, fused, limit=5)
            fused = self._merge_augmented(fused, augmented)

        # Step 6: Deduplicate and return top N
        seen = set()
        deduped = []
        for r in fused:
            if r["id"] not in seen:
                seen.add(r["id"])
                deduped.append(r)

        # Batch entity lookup: fetch all memory-entity links in one query
        if deduped and self.entity_store:
            entity_map = self._batch_get_memory_entities([r["id"] for r in deduped])
            for r in deduped:
                r["entities"] = entity_map.get(r["id"], [])

        return deduped[:limit]

    def _rrf_fuse(self, *result_lists) -> list[dict]:
        """
        Reciprocal Rank Fusion: merge multiple ranked lists.

        RRF score = Σ(1 / (k + rank_i)) for each list i

        This is a simple but effective fusion method that doesn't
        require score normalization between different strategies.
        """
        scores = {}  # id → {score, result_dict}

        for result_list in result_lists:
            for rank, result in enumerate(result_list, start=1):
                mem_id = result["id"]
                rrf_score = 1.0 / (RRF_K + rank)

                # Apply tier boost
                tier = result.get("tier", "active")
                boost = TIER_BOOST.get(tier, 1.0)
                rrf_score *= boost

                if mem_id in scores:
                    scores[mem_id]["score"] += rrf_score
                    scores[mem_id]["sources"].extend(result.get("sources", []))
                else:
                    scores[mem_id] = {
                        "score": rrf_score,
                        "result": result,
                        "sources": list(result.get("sources", [])),
                    }

        # Sort by fused score
        fused = []
        for mem_id, data in scores.items():
            result = data["result"].copy()
            result["score"] = data["score"]
            result["sources"] = list(set(data["sources"]))  # deduplicate
            fused.append(result)

        fused.sort(key=lambda x: x["score"], reverse=True)
        return fused

    def _graph_augment(self, query: str, base_results: list[dict], limit: int = 5) -> list[dict]:
        """
        Augment search results by following entity edges.

        For each entity found in top results, traverse the graph
        to find connected memories that might be relevant but
        weren't returned by keyword/vector search.
        """
        if not self.entity_store or not base_results:
            return []

        augmented = []
        seen_ids = {r["id"] for r in base_results}

        # Get entities from top results
        seed_entities = set()
        for result in base_results[:limit]:
            entities = self._get_memory_entities(result["id"])
            for e in entities:
                seed_entities.add(e)

        # Traverse graph from seed entities
        for entity_id in seed_entities:
            try:
                neighbors = self.entity_store.get_neighbors(entity_id, depth=1)
                for entity in neighbors.get("entities", []):
                    # Get memories linked to this entity
                    linked = self.entity_store.get_entity_memories(entity["id"], limit=5)
                    for mem in linked:
                        if mem["id"] not in seen_ids:
                            mem["score"] = 0.3  # Graph augmentation score (lower than direct match)
                            mem["sources"] = ["graph"]
                            augmented.append(mem)
                            seen_ids.add(mem["id"])
            except Exception as e:
                logger.debug(f"Graph augmentation failed for {entity_id}: {e}")

        return augmented

    def _merge_augmented(self, base: list[dict], augmented: list[dict]) -> list[dict]:
        """Merge augmented results into base results."""
        merged = list(base)
        for r in augmented:
            merged.append(r)
        merged.sort(key=lambda x: x["score"], reverse=True)
        return merged

    def _get_memory_entities(self, memory_id: str) -> list[str]:
        """Get entity IDs linked to a memory."""
        try:
            entities = self.entity_store.get_memory_entities(memory_id)
            return [e["id"] for e in entities]
        except Exception:
            return []

    def _batch_get_memory_entities(self, memory_ids: list[str]) -> dict[str, list[str]]:
        """Batch fetch memory-entity links. Single query instead of N+1."""
        if not memory_ids or not self.entity_store:
            return {}
        result = {}
        try:
            with _connect(self.entity_store.db_path) as conn:
                placeholders = ",".join("?" for _ in memory_ids)
                rows = conn.execute(
                    f"SELECT memory_id, entity_id FROM memory_entities WHERE memory_id IN ({placeholders})",
                    memory_ids,
                ).fetchall()
                for mem_id, entity_id in rows:
                    result.setdefault(mem_id, []).append(entity_id)
        except Exception:
            pass
        return result

    # ── Vector Utilities ─────────────────────────────────────────

    @staticmethod
    def _bytes_to_vector(data: bytes) -> list[float]:
        """Convert BLOB (float32 array) to Python list of floats."""
        if not data:
            return []
        try:
            count = len(data) // 4
            return list(struct.unpack(f"{count}f", data))
        except Exception:
            return []

    @staticmethod
    def _vector_to_bytes(vec: list[float]) -> bytes:
        """Convert Python list of floats to BLOB (float32 array)."""
        return struct.pack(f"{len(vec)}f", *vec)

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """
        Compute cosine similarity between two vectors.

        Returns value in [-1, 1]. Higher = more similar.
        """
        if len(a) != len(b) or len(a) == 0:
            return 0.0

        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))

        if norm_a == 0 or norm_b == 0:
            return 0.0

        return dot / (norm_a * norm_b)

    def build_context(
        self,
        query: str,
        project: Optional[str] = None,
        agent: Optional[str] = None,
        limit: int = 10,
    ) -> dict:
        """
        Build a context response for a task query.

        This is what Prism calls via `GET /context/build`.
        Returns structured context including memories, entities, and open threads.
        """
        results = self.search(query, mode="balanced", limit=limit)

        # Gather entity context from results
        all_entity_ids = set()
        for r in results:
            for eid in r.get("entities", []):
                all_entity_ids.add(eid)

        entity_context = []
        for eid in all_entity_ids:
            entity = self.entity_store.get_entity(eid)
            if entity:
                entity_context.append(
                    {
                        "id": eid,
                        "name": entity["name"],
                        "type": entity["type"],
                        "compiled_truth": entity.get("compiled_truth", ""),
                    }
                )

        # Gather open threads from top entities
        open_threads = []
        for entity in entity_context[:5]:
            timeline = entity.get("compiled_truth", "")
            if timeline:
                open_threads.append(
                    {
                        "entity": entity["name"],
                        "context": timeline[:200],
                    }
                )

        return {
            "query": query,
            "project": project,
            "agent": agent,
            "memories": results,
            "entities": entity_context,
            "open_threads": open_threads,
            "total_results": len(results),
        }
