"""
Entity Store — Entity Registry + Typed Edges

PATTERN: Graph Store (Entity-Relationship Model)
===================================================

Entities are the nodes in our knowledge graph. Each entity has:
- A canonical slug (unique ID, like "ema" or "prism")
- A type (person, project, concept, tool, decision, preference)
- Aliases (other names the entity goes by)
- Compiled truth (always-current synthesis)
- Timeline (append-only evidence log)

Edges connect entities with typed relationships:
- mentions: general reference
- decided_about: decision related to entity
- works_on: active work on entity
- related_to: loose connection
- depends_on: dependency relationship

This is inspired by gbrain's self-wiring knowledge graph:
every write extracts entity references and creates typed edges
with ZERO LLM calls (pure regex + lookup). The graph grows on
every capture at near-zero cost.

WHY ENTITIES SEPARATE FROM MEMORIES?
- A memory is a discrete event ("Ema decided Prism stays domain-agnostic")
- An entity is a persistent concept ("Ema", "Prism", "domain-agnostic")
- Multiple memories can reference the same entity
- Entity compiled truth is the SYNTHESIS of all memories about it
- Without entities, we have a bag of memories with no connections
"""

from __future__ import annotations

import json
import sqlite3
import time
import re
from contextlib import contextmanager
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from recall_mcp.store.migrations import run_migrations

logger = logging.getLogger(__name__)


# ── Entity Types ────────────────────────────────────────────────

ENTITY_TYPES = {"person", "project", "concept", "tool", "decision", "preference"}
EDGE_TYPES = {"mentions", "decided_about", "works_on", "related_to", "depends_on"}


@dataclass
class Entity:
    """A node in the knowledge graph."""

    id: str  # canonical slug: "ema", "prism"
    name: str  # display name: "Ema", "Prism"
    type: str  # person, project, concept, tool, decision, preference
    aliases: list[str]  # other names: ["Emmanuel", "rhem"]
    compiled_truth: str  # always-current synthesis
    timeline: str  # append-only evidence log
    tier: str  # cold, active, persist
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class Edge:
    """A typed relationship between two entities."""

    source_id: str
    target_id: str
    edge_type: str  # mentions, decided_about, works_on, related_to, depends_on
    since_date: float
    confidence: float = 1.0
    evidence: str = ""


class EntityStore:
    """
    SQLite-backed entity and edge store.

    The entity registry is the single source of truth for "is this the same
    entity?" — canonical IDs + aliases prevent duplicates. When two sources
    mention the same entity under different names, they resolve to one page.

    SCHEMA DESIGN:
    - entities: canonical ID, name, type, aliases (JSON), compiled truth, timeline
    - edges: typed relationships between entities (source, target, type, confidence)
    - memory_entities: links between memories and the entities they mention
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_tables()

    @contextmanager
    def _connect(self):
        """Open a transactional graph connection with foreign keys enabled."""
        connection = sqlite3.connect(str(self.db_path))
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _init_tables(self):
        """Install the shared canonical schema through the migration runner."""
        run_migrations(self.db_path)
        logger.info("Entity store initialized at %s", self.db_path)

    def create_entity(
        self,
        name: str,
        entity_type: str,
        aliases: list[str] | None = None,
        compiled_truth: str = "",
        timeline: str = "",
        tier: str = "active",
    ) -> str:
        """
        Create a new entity and return its canonical slug.

        DEDUP: Before creating, search for existing entities by name or alias.
        If found, update instead of creating a duplicate.
        """
        slug = self._slugify(name)

        # Check for existing entity
        existing = self.find_entity(name)
        if existing:
            logger.debug(f"Entity already exists: {existing['id']} ({name})")
            # Add alias if name differs
            existing_aliases = existing.get("aliases", [])
            name_lower = name.lower()
            entity_name_lower = existing["name"].lower()
            if name_lower != entity_name_lower and name not in existing_aliases:
                existing_aliases.append(name)
                self.update_entity(existing["id"], aliases=existing_aliases)
            return existing["id"]

        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO entities (id, name, type, aliases, compiled_truth, timeline, tier, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    slug,
                    name,
                    entity_type,
                    json.dumps(aliases or []),
                    compiled_truth,
                    timeline,
                    tier,
                    now,
                    now,
                ),
            )

            # Insert into alias lookup table for O(1) resolution
            name_lower = name.lower()
            conn.execute(
                "INSERT OR IGNORE INTO entity_aliases (alias, entity_id) VALUES (?, ?)",
                (name_lower, slug),
            )
            for alias in aliases or []:
                conn.execute(
                    "INSERT OR IGNORE INTO entity_aliases (alias, entity_id) VALUES (?, ?)",
                    (alias.lower(), slug),
                )

        logger.info(f"Created entity: {slug} (type={entity_type}, tier={tier})")
        return slug

    def get_entity(self, entity_id: str) -> Optional[dict]:
        """Get an entity by its canonical slug."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if row:
                d = dict(row)
                d["aliases"] = json.loads(d.get("aliases", "[]"))
                return d
        return None

    def update_entity(self, entity_id: str, **kwargs) -> bool:
        """Update entity fields. Only provided fields are changed."""
        allowed = {"name", "type", "aliases", "compiled_truth", "timeline", "tier"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return False

        # Serialize list fields
        if "aliases" in updates and isinstance(updates["aliases"], list):
            new_aliases = updates["aliases"]  # Keep list for alias table sync
            updates["aliases"] = json.dumps(updates["aliases"])
        else:
            new_aliases = None

        updates["updated_at"] = time.time()

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [entity_id]

        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE entities SET {set_clause} WHERE id = ?", values
            )
            # Keep alias table in sync if aliases were updated
            if new_aliases is not None:
                conn.execute(
                    "DELETE FROM entity_aliases WHERE entity_id = ?", (entity_id,)
                )
                # Re-add name as alias
                conn.execute(
                    "INSERT OR IGNORE INTO entity_aliases (alias, entity_id) VALUES (?, ?)",
                    (entity_id, entity_id),  # slug is the ID
                )
                for alias in new_aliases:
                    conn.execute(
                        "INSERT OR IGNORE INTO entity_aliases (alias, entity_id) VALUES (?, ?)",
                        (alias.lower(), entity_id),
                    )
            return cursor.rowcount > 0

    def add_timeline_entry(self, entity_id: str, entry: str, source: str = "") -> bool:
        """
        Append a timeline entry to an entity (append-only, never edit).

        FORMAT: "- **YYYY-MM-DD** | {entry} [Source: {source}]"
        """
        entity = self.get_entity(entity_id)
        if not entity:
            return False

        now = time.time()
        date_str = time.strftime("%Y-%m-%d", time.localtime(now))
        formatted = f"- **{date_str}** | {entry}"
        if source:
            formatted += f" [Source: {source}]"

        existing_timeline = entity.get("timeline", "")
        new_timeline = formatted + "\n" + existing_timeline  # newest first

        return self.update_entity(entity_id, timeline=new_timeline)

    def record_memory_mention(
        self,
        memory_id: str,
        entity_id: str,
        entry: str,
        *,
        source: str = "",
        confidence: float = 1.0,
    ) -> bool:
        """Atomically create one memory link and its timeline entry."""
        now = time.time()
        date_str = time.strftime("%Y-%m-%d", time.localtime(now))
        formatted = f"- **{date_str}** | {entry}"
        if source:
            formatted += f" [Source: {source}]"
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            entity = conn.execute(
                "SELECT timeline FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if entity is None:
                return False
            cursor = conn.execute(
                """
                INSERT INTO memory_entities (memory_id, entity_id, confidence)
                VALUES (?, ?, ?)
                ON CONFLICT(memory_id, entity_id) DO NOTHING
                """,
                (memory_id, entity_id, confidence),
            )
            if cursor.rowcount != 1:
                return False
            timeline = str(entity["timeline"] or "")
            conn.execute(
                """
                UPDATE entities SET timeline = ?, updated_at = ? WHERE id = ?
                """,
                (formatted + "\n" + timeline, now, entity_id),
            )
        return True

    def find_entity(self, name: str) -> Optional[dict]:
        """
        Find an entity by name or alias (fuzzy matching).

        Search order:
        1. Exact slug match
        2. Exact name match (case-insensitive)
        3. Alias match (case-insensitive)
        """
        slug = self._slugify(name)
        name_lower = name.lower()

        with self._connect() as conn:
            conn.row_factory = sqlite3.Row

            # 1. Exact slug
            row = conn.execute(
                "SELECT * FROM entities WHERE id = ?", (slug,)
            ).fetchone()
            if row:
                d = dict(row)
                d["aliases"] = json.loads(d.get("aliases", "[]"))
                return d

            # 2. Exact name (case-insensitive)
            row = conn.execute(
                "SELECT * FROM entities WHERE LOWER(name) = ?", (name_lower,)
            ).fetchone()
            if row:
                d = dict(row)
                d["aliases"] = json.loads(d.get("aliases", "[]"))
                return d

            # 3. Alias match — O(1) lookup via normalized alias table
            row = conn.execute(
                "SELECT entity_id FROM entity_aliases WHERE alias = ?", (name_lower,)
            ).fetchone()
            if row:
                return self.get_entity(row[0])

            # 4. Fallback: scan JSON aliases (for entities created before alias table)
            rows = conn.execute("SELECT id, aliases FROM entities").fetchall()
            for r in rows:
                aliases = json.loads(r["aliases"])
                if any(a.lower() == name_lower for a in aliases):
                    # Backfill the alias table
                    conn.execute(
                        "INSERT OR IGNORE INTO entity_aliases (alias, entity_id) VALUES (?, ?)",
                        (name_lower, r["id"]),
                    )
                    return self.get_entity(r["id"])

        return None

    def search_entities(
        self, query: str, entity_type: Optional[str] = None, limit: int = 10
    ) -> list[dict]:
        """Search entities by name/alias (keyword match)."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            sql = "SELECT * FROM entities WHERE 1=1"
            params: list[object] = []

            if query:
                sql += " AND (LOWER(name) LIKE ? OR LOWER(id) LIKE ?)"
                params.extend([f"%{query.lower()}%", f"%{query.lower()}%"])

            if entity_type:
                sql += " AND type = ?"
                params.append(entity_type)

            sql += " ORDER BY updated_at DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(sql, params).fetchall()
            results = []
            for r in rows:
                d = dict(r)
                d["aliases"] = json.loads(d.get("aliases", "[]"))
                results.append(d)
            return results

    def list_entities(
        self, entity_type: Optional[str] = None, limit: int = 50
    ) -> list[dict]:
        """List all entities, optionally filtered by type."""
        return self.search_entities("", entity_type=entity_type, limit=limit)

    def delete_entity(self, entity_id: str) -> bool:
        """Delete an entity and all its edges."""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM edges WHERE source_id = ? OR target_id = ?",
                (entity_id, entity_id),
            )
            conn.execute(
                "DELETE FROM memory_entities WHERE entity_id = ?", (entity_id,)
            )
            cursor = conn.execute("DELETE FROM entities WHERE id = ?", (entity_id,))
            return cursor.rowcount > 0

    # ── Edge CRUD ──────────────────────────────────────────────

    def add_edge(
        self,
        source_id: str,
        target_id: str,
        edge_type: str,
        confidence: float = 1.0,
        evidence: str = "",
    ) -> bool:
        """
        Create a typed edge between two entities.

        Ignores if edge already exists (idempotent).
        """
        if edge_type not in EDGE_TYPES:
            logger.warning(f"Unknown edge type: {edge_type}")
            return False

        now = time.time()
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO edges (source_id, target_id, edge_type, since_date, confidence, evidence)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_id, target_id, edge_type) DO NOTHING
                """,
                    (source_id, target_id, edge_type, now, confidence, evidence),
                )
                return True
            except sqlite3.IntegrityError as e:
                logger.debug(f"Edge already exists or FK violation: {e}")
                return False

    def get_edges(
        self, entity_id: str, direction: str = "both", edge_type: Optional[str] = None
    ) -> list[dict]:
        """
        Get edges for an entity.

        direction: 'outgoing' (entity is source), 'incoming' (entity is target),
                   'both' (default)
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row

            sql_parts = []
            params: list[object] = []

            if direction in ("outgoing", "both"):
                sql_parts.append("SELECT * FROM edges WHERE source_id = ?")
                params.append(entity_id)

            if direction in ("incoming", "both"):
                sql_parts.append("SELECT * FROM edges WHERE target_id = ?")
                params.append(entity_id)

            if not sql_parts:
                return []

            sql = " UNION ".join(sql_parts)
            if edge_type:
                # Wrap as subquery for type filter
                sql = f"SELECT * FROM ({sql}) WHERE edge_type = ?"
                params.append(edge_type)

            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def get_neighbors(
        self, entity_id: str, depth: int = 1, edge_types: Optional[list[str]] = None
    ) -> dict:
        """
        N-hop graph traversal from a seed entity.

        Returns:
        {
            "entities": [{id, name, type, ...}, ...],
            "edges": [{source_id, target_id, edge_type, ...}, ...],
            "center": entity_id
        }

        Uses BFS up to `depth` hops. Respects edge_types filter if provided.
        """
        visited_entities = set()
        visited_edges = set()
        queue = [entity_id]
        all_entities = []
        all_edges = []

        # Add center entity first
        center = self.get_entity(entity_id)
        if center:
            all_entities.append(center)
        visited_entities.add(entity_id)

        for _ in range(depth):
            next_queue = []
            for eid in queue:
                edges = self.get_edges(eid)
                for edge in edges:
                    edge_key = (edge["source_id"], edge["target_id"], edge["edge_type"])
                    if edge_key in visited_edges:
                        continue
                    if edge_types and edge["edge_type"] not in edge_types:
                        continue

                    visited_edges.add(edge_key)
                    all_edges.append(edge)

                    # Add neighbor
                    neighbor = (
                        edge["target_id"]
                        if edge["source_id"] == eid
                        else edge["source_id"]
                    )
                    if neighbor not in visited_entities:
                        visited_entities.add(neighbor)
                        entity = self.get_entity(neighbor)
                        if entity:
                            all_entities.append(entity)
                        next_queue.append(neighbor)

            queue = next_queue

        return {
            "entities": all_entities,
            "edges": all_edges,
            "center": entity_id,
        }

    def remove_edge(self, source_id: str, target_id: str, edge_type: str) -> bool:
        """Remove a specific edge."""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM edges WHERE source_id = ? AND target_id = ? AND edge_type = ?",
                (source_id, target_id, edge_type),
            )
            return cursor.rowcount > 0

    # ── Memory-Entity Links ────────────────────────────────────

    def link_memory_entity(
        self, memory_id: str, entity_id: str, confidence: float = 1.0
    ) -> bool:
        """Link a memory to an entity it mentions."""
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO memory_entities (memory_id, entity_id, confidence)
                    VALUES (?, ?, ?)
                    ON CONFLICT(memory_id, entity_id) DO NOTHING
                """,
                    (memory_id, entity_id, confidence),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def get_memory_entities(self, memory_id: str) -> list[dict]:
        """Get all entities linked to a memory."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT e.*, me.confidence as link_confidence
                FROM memory_entities me
                JOIN entities e ON me.entity_id = e.id
                WHERE me.memory_id = ?
            """,
                (memory_id,),
            ).fetchall()
            results = []
            for r in rows:
                d = dict(r)
                d["aliases"] = json.loads(d.get("aliases", "[]"))
                results.append(d)
            return results

    def get_entity_memories(self, entity_id: str, limit: int = 20) -> list[dict]:
        """Get all memories linked to an entity."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT m.*
                FROM memory_entities me
                JOIN memories m ON me.memory_id = m.id
                WHERE me.entity_id = ?
                ORDER BY m.created_at DESC
                LIMIT ?
            """,
                (entity_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Orphan Detection ───────────────────────────────────────

    def find_orphans(self, limit: int = 50) -> list[dict]:
        """Find entities with zero edges (disconnected from the graph)."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT e.* FROM entities e
                LEFT JOIN edges es ON e.id = es.source_id
                LEFT JOIN edges et ON e.id = et.target_id
                WHERE es.source_id IS NULL AND et.target_id IS NULL
                LIMIT ?
            """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Utilities ───────────────────────────────────────────────

    @staticmethod
    def _slugify(name: str) -> str:
        """
        Convert a name to a canonical slug.

        Rules:
        - Lowercase
        - Replace spaces with hyphens
        - Remove special characters (keep alphanumeric + hyphens)
        - Max 60 chars

        Examples:
            "Ema" → "ema"
            "AI Hedge Prism" → "ai-hedge-prism"
            "DilBERT Gate" → "dilbert-gate"
        """
        slug = name.lower().strip()
        slug = re.sub(r"[^a-z0-9\s-]", "", slug)
        slug = re.sub(r"[\s_]+", "-", slug)
        slug = slug.strip("-")
        return slug[:60]

    def stats(self) -> dict:
        """Get entity and edge counts."""
        with self._connect() as conn:
            entity_count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            edge_count = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            link_count = conn.execute(
                "SELECT COUNT(*) FROM memory_entities"
            ).fetchone()[0]
            orphan_count = len(self.find_orphans(limit=1000))

            type_counts = {}
            for row in conn.execute(
                "SELECT type, COUNT(*) FROM entities GROUP BY type"
            ):
                type_counts[row[0]] = row[1]

            edge_type_counts = {}
            for row in conn.execute(
                "SELECT edge_type, COUNT(*) FROM edges GROUP BY edge_type"
            ):
                edge_type_counts[row[0]] = row[1]

        return {
            "entities": entity_count,
            "edges": edge_count,
            "memory_entity_links": link_count,
            "orphans": orphan_count,
            "entities_by_type": type_counts,
            "edges_by_type": edge_type_counts,
        }
