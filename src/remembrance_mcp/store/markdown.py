"""
Markdown Sync — SQLite ↔ Brain Repo (Human-Readable Markdown Pages)

PATTERN: Generated View with Manual Edit Support
=================================================

SQLite is the source of truth. The brain repo at ~/.remembrance/brain/
is a human-readable generated view that users can also manually edit.

SYNC DIRECTION:
- SQLite → Markdown: Dream cycle exports entity pages
- Markdown → SQLite: Manual edits detected by mtime, picked up on next sync

Each entity page follows gbrain's two-layer format:
- Above the --- separator: always-current compiled truth + YAML frontmatter
- Below the --- separator: append-only chronological timeline

INSPIRED BY gbrain's "brain repo is git-backed markdown" pattern:
- DB is for retrieval, markdown is the human layer
- Git provides version history
- Manual edits are first-class (not overridden without detection)
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

from remembrance_mcp.store.edges import EntityStore

logger = logging.getLogger(__name__)


class MarkdownSync:
    """
    Sync entities between SQLite and the brain markdown repo.

    Usage:
        sync = MarkdownSync(entity_store, brain_dir=Path("~/.remembrance/brain"))
        sync.export_all()  # Export all entities to markdown
        sync.import_edits()  # Pick up manual edits
    """

    def __init__(self, entity_store: EntityStore, brain_dir: Optional[Path] = None):
        self.entity_store = entity_store
        self.brain_dir = brain_dir or Path.home() / ".remembrance" / "brain"
        self.brain_dir.mkdir(parents=True, exist_ok=True)

    def export_entity(self, entity_id: str) -> Optional[Path]:
        """
        Export a single entity to a markdown page.

        Creates the directory structure and writes the page.
        Returns the path to the created file, or None if entity not found.
        """
        entity = self.entity_store.get_entity(entity_id)
        if not entity:
            return None

        # Determine directory by entity type
        type_dirs = {
            "person": "people",
            "project": "projects",
            "concept": "concepts",
            "tool": "tools",
            "decision": "decisions",
            "preference": "preferences",
        }
        dir_name = type_dirs.get(entity["type"], "concepts")
        entity_dir = self.brain_dir / "entities" / dir_name
        entity_dir.mkdir(parents=True, exist_ok=True)

        # Build the markdown page
        content = self._build_page(entity)
        page_path = entity_dir / f"{entity_id}.md"

        # Only write if content changed (compare with existing)
        if page_path.exists():
            existing = page_path.read_text(encoding="utf-8")
            if existing == content:
                return page_path  # No change

        page_path.write_text(content, encoding="utf-8")
        logger.info(f"Exported entity: {entity_id} → {page_path}")
        return page_path

    def export_all(self) -> dict:
        """Export all entities to markdown pages."""
        entities = self.entity_store.list_entities(limit=500)
        exported = 0
        errors = 0

        for entity in entities:
            try:
                path = self.export_entity(entity["id"])
                if path:
                    exported += 1
            except Exception as e:
                logger.error(f"Failed to export {entity['id']}: {e}")
                errors += 1

        # Write brain README
        self._write_readme(entities)

        return {
            "exported": exported,
            "errors": errors,
            "total_entities": len(entities),
        }

    def import_edits(self) -> dict:
        """
        Scan brain repo for manually edited pages and update SQLite.

        Detects edits by comparing file mtime with entity.updated_at.
        Only updates compiled_truth (timeline is append-only, never edited).
        """
        imported = 0
        skipped = 0

        entities_dir = self.brain_dir / "entities"
        if not entities_dir.exists():
            return {"imported": 0, "skipped": 0}

        for md_file in entities_dir.rglob("*.md"):
            if md_file.name == "README.md":
                continue

            entity_id = md_file.stem
            entity = self.entity_store.get_entity(entity_id)

            if not entity:
                logger.debug(f"Skipping unknown entity: {entity_id}")
                skipped += 1
                continue

            # Check if file was modified after last entity update
            file_mtime = md_file.stat().st_mtime
            entity_updated = entity.get("updated_at", 0)

            if file_mtime <= entity_updated:
                continue  # File not modified since last export

            # Parse the compiled truth from the markdown
            compiled_truth = self._parse_compiled_truth(md_file)
            if compiled_truth and compiled_truth != entity.get("compiled_truth", ""):
                self.entity_store.update_entity(entity_id, compiled_truth=compiled_truth)
                # Add timeline entry about manual edit
                date_str = time.strftime("%Y-%m-%d", time.localtime(file_mtime))
                self.entity_store.add_timeline_entry(
                    entity_id,
                    "Compiled truth updated via manual edit",
                    source="markdown_sync",
                )
                imported += 1

        return {
            "imported": imported,
            "skipped": skipped,
        }

    def _build_page(self, entity: dict) -> str:
        """
        Build a markdown page for an entity.

        Format:
        ---
        type: person
        aliases: ["Emmanuel", "rhem"]
        tier: persist
        ---

        ## Executive Summary
        {compiled_truth}

        ## State
        (from facts store — future)

        ## Open Threads
        (from timeline — future)

        ---

        ## Timeline
        {timeline entries}
        """
        aliases = entity.get("aliases", [])
        aliases_str = json.dumps(aliases) if aliases else "[]"

        compiled_truth = entity.get("compiled_truth", "") or f"{entity['name']} — {entity['type']}"
        timeline = entity.get("timeline", "") or ""

        page = f"""---
type: {entity["type"]}
aliases: {aliases_str}
tier: {entity["tier"]}
---

## Executive Summary
{compiled_truth}

---

## Timeline
{timeline}
"""
        return page.strip() + "\n"

    def _parse_compiled_truth(self, md_path: Path) -> Optional[str]:
        """
        Extract compiled truth from a markdown page.

        Looks for content between "## Executive Summary" and the next
        "---" or "##" heading.
        """
        content = md_path.read_text(encoding="utf-8")
        lines = content.split("\n")

        in_summary = False
        summary_lines = []

        for line in lines:
            if line.strip() == "## Executive Summary":
                in_summary = True
                continue
            if in_summary:
                if line.strip().startswith("##") or line.strip() == "---":
                    break
                summary_lines.append(line)

        truth = "\n".join(summary_lines).strip()
        return truth if truth else None

    def _write_readme(self, entities: list[dict]):
        """Write a brain README with stats and index."""
        type_counts = {}
        for e in entities:
            t = e["type"]
            type_counts[t] = type_counts.get(t, 0) + 1

        now = time.strftime("%Y-%m-%d %H:%M", time.localtime())

        lines = [
            "# Remembrance Brain",
            "",
            f"Generated: {now}",
            f"Total entities: {len(entities)}",
            "",
            "## Entity Counts",
            "",
        ]
        for etype, count in sorted(type_counts.items()):
            lines.append(f"- **{etype}**: {count}")

        lines.extend(["", "## Directory Structure", ""])
        lines.append("- `entities/people/` — Person entities")
        lines.append("- `entities/projects/` — Project entities")
        lines.append("- `entities/concepts/` — Concept entities")
        lines.append("- `entities/tools/` — Tool entities")
        lines.append("- `entities/decisions/` — Decision entities")
        lines.append("- `entities/preferences/` — Preference entities")
        lines.append("")
        lines.append("Each page follows the two-layer format:")
        lines.append("- Above `---`: always-current compiled truth")
        lines.append("- Below `---`: append-only chronological timeline")
        lines.append("")

        readme_path = self.brain_dir / "README.md"
        readme_path.write_text("\n".join(lines), encoding="utf-8")
