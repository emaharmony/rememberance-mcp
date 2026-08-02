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

import re
import math
import sqlite3
import struct
import time
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

from recall_mcp.store.edges import EntityStore
from recall_mcp.store.facts import FactStore
from recall_mcp.embeddings import EmbeddingError, EmbeddingProvider

logger = logging.getLogger(__name__)


@contextmanager
def _connect(db_path: Path):
    """Open a SQLite connection that commits/rolls back and closes on Windows."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
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

    def __init__(
        self,
        db_path: Path,
        entity_store: EntityStore,
        embedding_provider: Optional[EmbeddingProvider] = None,
        fact_store: FactStore | None = None,
    ):
        self.db_path = db_path
        self.entity_store = entity_store
        self.embedding_provider = embedding_provider
        self.fact_store = fact_store

    @staticmethod
    def _scope_columns(conn: sqlite3.Connection) -> set[str]:
        return {
            row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()
        }

    @staticmethod
    def _formal_scope(
        user_id: Optional[str],
        workspace_id: Optional[str],
        project_id: Optional[str],
        repository_id: Optional[str],
        task_id: Optional[str],
        session_id: Optional[str],
    ) -> dict[str, str]:
        return {
            key: value
            for key, value in {
                "user_id": user_id,
                "workspace_id": workspace_id,
                "project_id": project_id,
                "repository_id": repository_id,
                "task_id": task_id,
                "session_id": session_id,
            }.items()
            if value is not None
        }

    @staticmethod
    def _append_scope_sql(
        sql: str,
        params: list[object],
        scope: dict[str, str],
        *,
        prefix: str = "",
    ) -> str:
        for column, value in scope.items():
            sql += f" AND {prefix}{column} = ?"
            params.append(value)
        return sql

    @staticmethod
    def _append_eligibility_sql(
        sql: str,
        params: list[object],
        columns: set[str],
        *,
        prefix: str = "",
        include_cold: bool = False,
    ) -> str:
        now = time.time()
        if "lifecycle_state" not in columns:
            sql += f" AND ({prefix}expires_at IS NULL OR {prefix}expires_at > ?)"
            params.append(now)
            return sql
        if include_cold:
            sql += (
                f" AND (({prefix}expires_at IS NULL OR {prefix}expires_at > ?)"
                f" OR {prefix}lifecycle_state = 'cold')"
            )
            params.append(now)
        else:
            sql += f" AND {prefix}lifecycle_state NOT IN ('cold', 'archived')"
            sql += f" AND ({prefix}expires_at IS NULL OR {prefix}expires_at > ?)"
            params.append(now)
        return sql

    def search(
        self,
        query: str,
        mode: str = "balanced",
        category: Optional[str] = None,
        tier: Optional[str] = None,
        limit: int = 10,
        project: Optional[str] = None,
        agent: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
        repository_id: Optional[str] = None,
        task_id: Optional[str] = None,
        session_id: Optional[str] = None,
        include_cold: bool = False,
    ) -> list[dict]:
        """
        Search memories using hybrid retrieval.

        Modes:
        - "keyword": FTS5 only (fastest, cheapest)
        - "vector": Cosine similarity only
        - "balanced": FTS5 + vector + tier boost + graph (DEFAULT)
        - "deep": balanced + entity aliases and compiled-truth expansion

        Returns list of result dicts sorted by fused score.
        """
        scope = self._formal_scope(
            user_id, workspace_id, project_id, repository_id, task_id, session_id
        )
        if mode == "keyword":
            return self._search_keyword(
                query, category, tier, limit, project, agent, scope, include_cold
            )
        elif mode == "vector":
            return self._search_vector(
                query, category, limit, tier, project, agent, scope, include_cold
            )
        elif mode == "balanced":
            return self._search_balanced(
                query, category, tier, limit, project, agent, scope, include_cold
            )
        elif mode == "deep":
            return self._search_deep(
                query,
                category,
                tier,
                limit,
                project,
                agent,
                scope,
                include_cold,
            )
        else:
            return self._search_balanced(
                query, category, tier, limit, project, agent, scope, include_cold
            )

    def _search_keyword(
        self,
        query: str,
        category: Optional[str] = None,
        tier: Optional[str] = None,
        limit: int = 10,
        project: Optional[str] = None,
        agent: Optional[str] = None,
        formal_scope: Optional[dict[str, str]] = None,
        include_cold: bool = False,
    ) -> list[dict]:
        """FTS5 full-text search only."""
        results = []
        with _connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            scope_columns = self._scope_columns(conn)
            try:
                # Escape FTS5 special chars: hyphens become spaces, quotes escaped
                safe_query = query.replace("-", " ").replace('"', '""')
                sql = """
                    SELECT m.*, fts.rank as fts_rank
                    FROM memories m
                    JOIN memories_fts fts ON m.rowid = fts.rowid
                    WHERE memories_fts MATCH ?
                """
                params: list[object] = [safe_query]

                if category:
                    sql += " AND m.category = ?"
                    params.append(category)
                if tier:
                    sql += " AND m.tier = ?"
                    params.append(tier)

                if project and "project" in scope_columns:
                    sql += " AND (m.project = ? OR m.project = '')"
                    params.append(project)
                if agent and "agent" in scope_columns:
                    sql += " AND (m.agent = ? OR m.agent = '')"
                    params.append(agent)
                sql = self._append_scope_sql(
                    sql, params, formal_scope or {}, prefix="m."
                )
                sql = self._append_eligibility_sql(
                    sql,
                    params,
                    scope_columns,
                    prefix="m.",
                    include_cold=include_cold,
                )

                sql += " ORDER BY fts.rank LIMIT ?"
                params.append(limit)

                rows = conn.execute(sql, params).fetchall()
                for r in rows:
                    d = dict(r)
                    d["score"] = 1.0 / (
                        1.0 + abs(d.pop("fts_rank", 0))
                    )  # FTS rank is negative (lower = better)
                    d["keyword_score"] = d["score"]
                    d["sources"] = ["fts5"]
                    results.append(d)
            except Exception as e:
                if isinstance(e, sqlite3.OperationalError):
                    logger.debug(
                        f"FTS5 search failed (OperationalError): {e}, using LIKE fallback"
                    )
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

                if project and "project" in scope_columns:
                    sql += " AND (project = ? OR project = '')"
                    params.append(project)
                if agent and "agent" in scope_columns:
                    sql += " AND (agent = ? OR agent = '')"
                    params.append(agent)
                sql = self._append_scope_sql(sql, params, formal_scope or {})
                sql = self._append_eligibility_sql(
                    sql, params, scope_columns, include_cold=include_cold
                )

                sql += " ORDER BY accessed_at DESC LIMIT ?"
                params.append(limit)

                rows = conn.execute(sql, params).fetchall()
                for r in rows:
                    d = dict(r)
                    d["score"] = 0.5  # Lower than FTS5 matches
                    d["keyword_score"] = d["score"]
                    d["sources"] = ["like"]
                    results.append(d)

        return results

    def _search_vector(
        self,
        query: str,
        category: Optional[str] = None,
        limit: int = 10,
        tier: Optional[str] = None,
        project: Optional[str] = None,
        agent: Optional[str] = None,
        formal_scope: Optional[dict[str, str]] = None,
        include_cold: bool = False,
    ) -> list[dict]:
        """
        Vector similarity search using cosine similarity on embedding BLOBs.

        The configured provider embeds the query. If it is unavailable,
        retrieval falls back to keyword search.
        """
        # Query embeddings come from the configured provider.
        # Keyword fallback keeps retrieval available during provider outages.
        if self.embedding_provider is None:
            return self._search_keyword(
                query,
                category,
                tier,
                limit,
                project,
                agent,
                formal_scope,
                include_cold,
            )
        try:
            embedded = self.embedding_provider.embed(query)
        except EmbeddingError as exc:
            logger.info("Vector query unavailable; using keyword search: %s", exc)
            return self._search_keyword(
                query,
                category,
                tier,
                limit,
                project,
                agent,
                formal_scope,
                include_cold,
            )
        return self.search_with_embedding(
            embedded.to_bytes(),
            category=category,
            tier=tier,
            project=project,
            agent=agent,
            formal_scope=formal_scope,
            include_cold=include_cold,
            limit=limit,
        )

    def search_with_embedding(
        self,
        query_embedding: bytes,
        category: Optional[str] = None,
        limit: int = 10,
        tier: Optional[str] = None,
        project: Optional[str] = None,
        agent: Optional[str] = None,
        formal_scope: Optional[dict[str, str]] = None,
        include_cold: bool = False,
    ) -> list[dict]:
        """
        Search using a pre-computed embedding vector.

        Args:
            query_embedding: The query vector as bytes (float32 array)
            category: Optional category filter
            limit: Maximum results

        Returns:
            List of results sorted by cosine similarity
        """
        query_vec = self._bytes_to_vector(query_embedding)
        if not query_vec:
            return []

        candidates = []
        with _connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            scope_columns = self._scope_columns(conn)
            where_sql = """
                FROM memories
                WHERE embedding IS NOT NULL
            """
            params: list[object] = []
            if "embedding_dimensions" in scope_columns:
                where_sql += (
                    " AND (embedding_dimensions IS NULL OR embedding_dimensions = ?)"
                )
                params.append(len(query_vec))
            if category:
                where_sql += " AND category = ?"
                params.append(category)
            if tier:
                where_sql += " AND tier = ?"
                params.append(tier)
            if project and "project" in scope_columns:
                where_sql += " AND (project = ? OR project = '')"
                params.append(project)
            if agent and "agent" in scope_columns:
                where_sql += " AND (agent = ? OR agent = '')"
                params.append(agent)
            where_sql = self._append_scope_sql(where_sql, params, formal_scope or {})
            where_sql = self._append_eligibility_sql(
                where_sql, params, scope_columns, include_cold=include_cold
            )

            rows = None
            sqlite_vec_enabled = False
            try:
                import sqlite_vec

                conn.enable_load_extension(True)
                try:
                    sqlite_vec.load(conn)
                finally:
                    conn.enable_load_extension(False)
                vector_sql = (
                    "SELECT *, vec_distance_cosine(embedding, ?) "
                    "AS vector_distance "
                    + where_sql
                    + " ORDER BY vector_distance ASC LIMIT 500"
                )
                rows = conn.execute(vector_sql, [query_embedding, *params]).fetchall()
                sqlite_vec_enabled = True
            except (ImportError, AttributeError, sqlite3.Error) as exc:
                logger.debug(
                    "sqlite-vec unavailable; using exact Python cosine: %s", exc
                )

            if rows is None:
                rows = conn.execute(
                    "SELECT * " + where_sql + " ORDER BY accessed_at DESC LIMIT 500",
                    params,
                ).fetchall()

            for row in rows:
                if sqlite_vec_enabled:
                    similarity = 1.0 - float(row["vector_distance"])
                    source = "sqlite-vec"
                else:
                    mem_vec = self._bytes_to_vector(row["embedding"])
                    if not mem_vec:
                        continue
                    similarity = self._cosine_similarity(query_vec, mem_vec)
                    source = "vector"

                tier_boost = TIER_BOOST.get(row["tier"], 1.0)
                boosted_score = similarity * tier_boost
                candidates.append(
                    {
                        "id": row["id"],
                        "content": row["content"],
                        "compiled_truth": row["compiled_truth"] or "",
                        "summary": row["summary"] or "",
                        "category": row["category"],
                        "tier": row["tier"],
                        "score": boosted_score,
                        "vector_score": similarity,
                        "tier_boost": tier_boost,
                        "sources": [source],
                    }
                )

        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[:limit]

    def _search_deep(
        self,
        query: str,
        category: Optional[str] = None,
        tier: Optional[str] = None,
        limit: int = 10,
        project: Optional[str] = None,
        agent: Optional[str] = None,
        formal_scope: Optional[dict[str, str]] = None,
        include_cold: bool = False,
    ) -> list[dict]:
        """Expand recognized entities and fuse the resulting hybrid searches."""
        expanded_terms: list[str] = []
        if self.entity_store:
            for token in re.findall(r"[A-Za-z][\w.-]{2,}", query):
                entity = self.entity_store.find_entity(token)
                if not entity:
                    continue
                expanded_terms.append(entity["name"])
                expanded_terms.extend(entity.get("aliases", [])[:3])
                truth = (entity.get("compiled_truth") or "").strip()
                if truth:
                    expanded_terms.extend(truth.split()[:12])

        queries = [query]
        if expanded_terms:
            expansion = " ".join(dict.fromkeys(expanded_terms))
            if expansion.lower() != query.lower():
                queries.append(expansion)

        result_sets = [
            self._search_balanced(
                candidate,
                category,
                tier,
                max(limit, 30),
                project,
                agent,
                formal_scope,
                include_cold,
            )
            for candidate in queries
        ]
        fused = self._rrf_fuse(*result_sets)
        return fused[:limit]

    def _search_balanced(
        self,
        query: str,
        category: Optional[str] = None,
        tier: Optional[str] = None,
        limit: int = 10,
        project: Optional[str] = None,
        agent: Optional[str] = None,
        formal_scope: Optional[dict[str, str]] = None,
        include_cold: bool = False,
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
        fts_results = self._search_keyword(
            query,
            category,
            tier,
            30,
            project,
            agent,
            formal_scope,
            include_cold,
        )

        # Step 2: semantic vector retrieval with keyword fallback
        vec_results = self._search_vector(
            query,
            category,
            30,
            tier,
            project,
            agent,
            formal_scope,
            include_cold,
        )

        # Step 3+4: RRF fusion
        fused = self._rrf_fuse(fts_results, vec_results)

        # Step 5: Graph augmentation
        if self.entity_store:
            augmented = self._graph_augment(
                query,
                fused,
                limit=5,
                formal_scope=formal_scope,
                include_cold=include_cold,
            )
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
        scores: dict[str, dict] = {}  # id → {score, result_dict}

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
                    for component in ("keyword_score", "vector_score", "graph_score"):
                        if result.get(component) is not None:
                            scores[mem_id]["components"][component] = result[component]
                else:
                    scores[mem_id] = {
                        "score": rrf_score,
                        "result": result,
                        "sources": list(result.get("sources", [])),
                        "components": {
                            component: result[component]
                            for component in (
                                "keyword_score",
                                "vector_score",
                                "graph_score",
                            )
                            if result.get(component) is not None
                        },
                    }

        # Sort by fused score
        fused = []
        for mem_id, data in scores.items():
            result = data["result"].copy()
            result["score"] = data["score"]
            result["sources"] = list(set(data["sources"]))  # deduplicate
            result.update(data["components"])
            result["tier_boost"] = TIER_BOOST.get(result.get("tier", "active"), 1.0)
            fused.append(result)

        fused.sort(key=lambda x: x["score"], reverse=True)
        return fused

    def _graph_augment(
        self,
        query: str,
        base_results: list[dict],
        limit: int = 5,
        formal_scope: Optional[dict[str, str]] = None,
        include_cold: bool = False,
    ) -> list[dict]:
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

        direct_scores = [float(r.get("score", 0.0)) for r in base_results]
        graph_score = min(direct_scores) * 0.5 if direct_scores else 0.0
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
                    linked = self.entity_store.get_entity_memories(
                        entity["id"],
                        limit=5,
                        formal_scope=formal_scope,
                        include_cold=include_cold,
                    )
                    for mem in linked:
                        if mem["id"] not in seen_ids:
                            mem["score"] = graph_score
                            mem["graph_score"] = graph_score
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
        result: dict[str, list[str]] = {}
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
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
        repository_id: Optional[str] = None,
        task_id: Optional[str] = None,
        session_id: Optional[str] = None,
        include_cold: bool = False,
        results: Optional[list[dict]] = None,
    ) -> dict:
        """
        Build a context response for a task query.

        This is what Prism calls via `GET /context/build`.
        Returns structured context including memories, entities, and open threads.
        """
        formal_scope = self._formal_scope(
            user_id, workspace_id, project_id, repository_id, task_id, session_id
        )
        if results is None:
            results = self.search(
                query,
                mode="balanced",
                project=project,
                agent=agent,
                limit=limit,
                user_id=user_id,
                workspace_id=workspace_id,
                project_id=project_id,
                repository_id=repository_id,
                task_id=task_id,
                session_id=session_id,
                include_cold=include_cold,
            )

        # Gather entity context from results
        all_entity_ids = set()
        for r in results:
            for eid in r.get("entities", []):
                all_entity_ids.add(eid)

        entity_context = []
        for eid in all_entity_ids if not formal_scope else ():
            entity = self.entity_store.get_entity(eid)
            if entity:
                entity_context.append(
                    {
                        "id": eid,
                        "name": entity["name"],
                        "type": entity["type"],
                        "compiled_truth": entity.get("compiled_truth", ""),
                        "facts": self.fact_store.get_entity_facts(eid)
                        if self.fact_store
                        else [],
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
            "scope": formal_scope,
            "memories": results,
            "entities": entity_context,
            "open_threads": open_threads,
            "total_results": len(results),
        }
