"""
Graph Wiring — Connect Entities to Memories and Each Other

PATTERN: Graph Construction on Every Write
============================================

Every capture() call runs entity detection and wires the results
into the knowledge graph. This module orchestrates:

1. Entity detection from the captured text
2. Entity creation (if new) or alias update (if existing)
3. Edge creation between entities mentioned together
4. Memory-entity link creation
5. Timeline entry creation on entities

This is the "self-wiring" part of gbrain's self-wiring knowledge graph:
the graph grows as a side effect of normal operations, not as a
separate task.
"""

from __future__ import annotations

import logging
from recall_mcp.graph.entity import EntityDetector
from recall_mcp.store.edges import EntityStore

logger = logging.getLogger(__name__)


class GraphWiring:
    """
    Wire entities into the knowledge graph on every capture.

    Usage:
        wiring = GraphWiring(entity_store)
        result = wiring.wire("Ema decided Prism stays domain-agnostic", memory_id="mem_123")
        # → Creates/updates entities "ema" and "prism", creates edge (ema, decided_about, prism)
    """

    def __init__(self, entity_store: EntityStore):
        self.entity_store = entity_store
        self.detector = EntityDetector(entity_store=entity_store)

    def wire(self, text: str, memory_id: str, source: str = "") -> dict:
        """
        Detect entities in text and wire them into the graph.

        Returns a dict with:
        - entities: list of entity slugs created/updated
        - edges: list of edges created
        - new_entities: list of newly created entity slugs
        - links: number of memory-entity links created
        """
        # Step 1: Detect entities
        detected = self.detector.detect(text)

        if not detected:
            return {
                "entities": [],
                "edges": [],
                "new_entities": [],
                "links": 0,
            }

        # Step 2: Create/update entities and collect slugs
        entity_ids = []
        new_entities = []

        for de in detected:
            existing = self.entity_store.find_entity(de.name)

            if existing:
                entity_id = existing["id"]
            else:
                # Create new entity
                entity_id = self.entity_store.create_entity(
                    name=de.name,
                    entity_type=de.entity_type,
                    compiled_truth=f"{de.name} — {de.entity_type}",
                    timeline="",
                    tier="active" if de.confidence >= 0.7 else "cold",
                )
                new_entities.append(entity_id)

            entity_ids.append(entity_id)

        # Step 3: Create edges between co-mentioned entities
        edges_created = []
        for de, entity_id in zip(detected, entity_ids):
            # Find the primary target of the edge (the other entity mentioned)
            for other_de, other_id in zip(detected, entity_ids):
                if entity_id == other_id:
                    continue
                if de.edge_type == "mentions":
                    continue  # Don't create mention edges between co-mentioned entities

                # Create the edge
                created = self.entity_store.add_edge(
                    source_id=entity_id,
                    target_id=other_id,
                    edge_type=de.edge_type,
                    confidence=de.confidence,
                    evidence=de.context,
                )
                if created:
                    edges_created.append(
                        {
                            "source": entity_id,
                            "target": other_id,
                            "type": de.edge_type,
                        }
                    )

        # Step 4: Link each entity and append its timeline exactly once.
        links = 0
        new_entity_ids = set(new_entities)
        for detected_entity, entity_id in zip(detected, entity_ids):
            if entity_id in new_entity_ids:
                timeline_entry = (
                    f"Entity created from memory {memory_id}: "
                    f'"{detected_entity.context}"'
                )
            else:
                timeline_entry = f"Mentioned in memory {memory_id}"
                if detected_entity.context:
                    timeline_entry += f': "{detected_entity.context}"'
            if self.entity_store.record_memory_mention(
                memory_id,
                entity_id,
                timeline_entry,
                source=source,
                confidence=detected_entity.confidence,
            ):
                links += 1

        # Step 5: Always add "mentions" edges from each entity to the others
        # This creates the base connectivity even without decision patterns
        # Scale limit: cap at 10 detected entities to avoid O(n²) explosion
        mention_limit = 10
        for i, (de, entity_id) in enumerate(
            zip(detected[:mention_limit], entity_ids[:mention_limit])
        ):
            for j, (other_de, other_id) in enumerate(
                zip(detected[:mention_limit], entity_ids[:mention_limit])
            ):
                if i >= j:
                    continue  # avoid duplicates
                created = self.entity_store.add_edge(
                    source_id=entity_id,
                    target_id=other_id,
                    edge_type="mentions",
                    confidence=min(de.confidence, other_de.confidence),
                    evidence=f"Co-mentioned in memory {memory_id}",
                )
                if created:
                    edges_created.append(
                        {
                            "source": entity_id,
                            "target": other_id,
                            "type": "mentions",
                        }
                    )
                # Also create reverse mention
                self.entity_store.add_edge(
                    source_id=other_id,
                    target_id=entity_id,
                    edge_type="mentions",
                    confidence=min(de.confidence, other_de.confidence),
                    evidence=f"Co-mentioned in memory {memory_id}",
                )

        logger.info(
            f"Graph wiring: {len(detected)} entities detected, "
            f"{len(new_entities)} new, {len(edges_created)} edges, {links} links"
        )

        return {
            "entities": entity_ids,
            "edges": edges_created,
            "new_entities": new_entities,
            "links": links,
        }
