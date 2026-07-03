from __future__ import annotations

"""
Graph Traversal — N-Hop Queries on the Knowledge Graph

PATTERN: BFS Graph Traversal
=============================

Starting from a seed entity, traverse the graph up to N hops
to find related entities and the paths connecting them.

This is what makes the knowledge graph useful for recall:
"Show me everything related to Prism" becomes a traversal
that finds people, decisions, tools, and concepts connected
to Prism through typed edges.

INSPIRED BY gbrain's graph-query: the +31 P@5 lift over
vector-only search comes from graph traversal finding
connections that embeddings can't see.
"""

import logging

from remembrance_mcp.store.edges import EntityStore

logger = logging.getLogger(__name__)


class GraphTraversal:
    """
    Traverse the knowledge graph from a seed entity.

    Usage:
        traversal = GraphTraversal(entity_store)
        result = traversal.query("prism", depth=2)
        # → Returns all entities within 2 hops + connecting edges
    """

    def __init__(self, entity_store: EntityStore):
        self.entity_store = entity_store

    def query(self, entity_id: str, depth: int = 1, edge_types: list[str] | None = None) -> dict:
        """
        N-hop graph traversal from a seed entity.

        Args:
            entity_id: Canonical slug to start from
            depth: Number of hops (1 = direct neighbors, 2 = neighbors of neighbors)
            edge_types: Filter to specific edge types (None = all)

        Returns:
            {
                "center": entity_id,
                "entities": list of entity dicts,
                "edges": list of edge dicts,
                "depth_reached": actual depth reached
            }
        """
        return self.entity_store.get_neighbors(entity_id, depth=depth, edge_types=edge_types)

    def find_path(self, from_id: str, to_id: str, max_depth: int = 4) -> list[dict] | None:
        """
        Find a path between two entities (BFS shortest path).

        Returns list of edges forming the path, or None if no path found.
        Useful for: "How is Ema connected to DilBERT?"
        """
        if from_id == to_id:
            return []

        # BFS
        visited = {from_id}
        queue = [(from_id, [])]  # (current_entity, path_so_far)

        for _ in range(max_depth):
            next_queue = []
            for current, path in queue:
                edges = self.entity_store.get_edges(current)
                for edge in edges:
                    neighbor = (
                        edge["target_id"] if edge["source_id"] == current else edge["source_id"]
                    )
                    if neighbor in visited:
                        continue
                    visited.add(neighbor)
                    new_path = path + [edge]
                    if neighbor == to_id:
                        return new_path
                    next_queue.append((neighbor, new_path))
            queue = next_queue

        return None  # No path found within max_depth

    def get_context(self, entity_id: str, depth: int = 1) -> str:
        """
        Build a human-readable context string from graph traversal.

        Useful for injecting into LLM prompts: "Here's what's connected to Prism..."
        """
        result = self.query(entity_id, depth=depth)
        if not result["entities"]:
            return f"No entities found near {entity_id}."

        lines = [f"**Knowledge graph context for {entity_id}:**"]

        for entity in result["entities"]:
            if entity["id"] == entity_id:
                continue
            edge_desc = self._describe_relationship(entity_id, entity["id"], result["edges"])
            truth = entity.get("compiled_truth", "")
            truth_line = f" — {truth}" if truth else ""
            lines.append(f"- {entity['name']} ({entity['type']}) {edge_desc}{truth_line}")

        return "\n".join(lines)

    def _describe_relationship(self, from_id: str, to_id: str, edges: list[dict]) -> str:
        """Describe the relationship between two entities from edge data."""
        for edge in edges:
            if (edge["source_id"] == from_id and edge["target_id"] == to_id) or (
                edge["source_id"] == to_id and edge["target_id"] == from_id
            ):
                edge_type = edge["edge_type"].replace("_", " ")
                direction = "→" if edge["source_id"] == from_id else "←"
                return f"[{direction} {edge_type}]"
        return "[connected]"
