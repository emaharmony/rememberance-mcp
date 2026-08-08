# Migrating to Recall

Recall is the canonical identity for the project formerly named Remembrance. This rename does not require a database schema migration and does not move user data automatically.

## Naming map

| Previous | Canonical |
| --- | --- |
| Remembrance | Recall |
| remembrance-mcp distribution | recall-mcp |
| remembrance_mcp package | recall_mcp |
| remembrance-mcp CLI | recall-mcp |
| remembrance-service CLI | recall-service |
| REMEMBRANCE_ prefix | RECALL_ prefix |
| ~/.remembrance | ~/.recall |

## Python package migration

Update application imports:

~~~python
from recall_mcp import MemoryPipeline
from recall_mcp.search.hybrid import HybridSearch
~~~

The old package and currently used nested submodules remain thin aliases to the Recall implementation:

~~~python
from remembrance_mcp.pipeline import MemoryPipeline
from remembrance_mcp.search.hybrid import HybridSearch
~~~

Legacy imports emit a compatibility warning to stderr. No implementation is duplicated under the old namespace.

## Command migration

Use:

~~~bash
recall --help
recall-mcp --help
recall-service --help
python -m recall_mcp --help
~~~

Temporarily supported deprecated forms are:

~~~bash
remembrance-mcp --help
remembrance-service --help
python -m remembrance_mcp --help
~~~

## Environment variables

Rename variables while keeping values unchanged:

| Deprecated | Canonical |
| --- | --- |
| REMEMBRANCE_HOME | RECALL_HOME |
| REMEMBRANCE_GATE_BACKENDS | RECALL_GATE_BACKENDS |
| REMEMBRANCE_URL | RECALL_URL |
| REMEMBRANCE_TIMEOUT | RECALL_TIMEOUT |
| REMEMBRANCE_INJECT_LIMIT | RECALL_INJECT_LIMIT |

Resolution order is canonical variable, matching deprecated variable, then application default. When both are set, RECALL_* wins. Warnings never include values or credentials.

## Home directory and databases

Recall resolves its data directory in this order:

1. Explicit RECALL_HOME
2. Existing ~/.recall
3. Explicit REMEMBRANCE_HOME
4. Existing ~/.remembrance
5. New ~/.recall

If an existing legacy directory is selected, Recall keeps using it in place. It does not move, copy, rename, overwrite, or delete the database. It does not create a second empty database. SQLite table names, IDs, vectors, embeddings, facts, graph data, and dream records remain unchanged.

To migrate data later, stop all Recall processes, make a verified backup, move the directory yourself, and set RECALL_HOME explicitly. Automatic data movement is intentionally out of scope for this release.

## Legacy database import (divergent schema history)

The in-place approach above assumes the existing database's recorded migration history matches a prefix of the current migration sequence, so `recall-admin migrate` can bring it forward. Some older stores don't satisfy that: they accumulated migrations under names that don't match any current migration (for example, a recorded history of `6:capture_idempotency, 7:structured_capture_errors` where the current schema's versions 6 and 7 are named differently), or otherwise drifted across incompatible schema lineages. `run_migrations` correctly refuses to touch such a database rather than guessing at a repair, which means `recall-admin migrate` (and therefore just pointing `RECALL_HOME` at it) is not an option.

For that situation, `recall-admin import-legacy` reads the legacy database's raw tables directly — without ever running its migration history — and writes a clean copy of what's salvageable into a current-schema target:

~~~bash
recall-admin import-legacy --source /path/to/old/memory.db --target-home ~/.recall --dry-run
~~~

Run with `--dry-run` first. It writes nothing anywhere (not the source, never; not the target either) and prints a full plan: counts of memories that would import, that are already present (idempotency), that are skipped for being expired, exact-duplicate, or (if requested) near-duplicate content, plus how many orphaned chunks would be salvaged and the byte totals and date range covered. Review the plan, then drop `--dry-run` to write it for real.

### What it carries over

- **`memories`** — content, summary, category, tier, key topics, source, timestamps, compiled truth, timeline, dream metadata, project/agent scope, and access/processing status, mapped 1:1 onto the current columns of the same name. The original `id` and `created_at` are preserved, which makes re-running the import idempotent: every legacy id is checked against the target before insertion, so a second run imports zero new rows (reported separately from rows skipped by a filter).
- **`raw_captures`** — imported as supplementary provenance wherever the current schema can accept it cleanly. A capture whose `memory_id` points at a memory that wasn't imported (filtered out, or never existed) is kept anyway, with `memory_id` cleared, rather than dropped or forced onto a foreign key that would fail.
- **Orphaned `memory_chunks`** — chunk rows whose parent memory row no longer exists (typically because a dream-purge cycle failed to cascade the delete) are the *only* surviving copy of that content. They're grouped by `memory_id`, ordered by `chunk_index`, concatenated, and reinserted as one memory under the original `memory_id`, with `source` set to `legacy-salvage` and a note in the summary explaining the recovery.

### What it deliberately discards, and why

- **Legacy embeddings** (`embedding`, `embedding_dim`/`embedding_dimensions`, `embedding_model`, `embedding_content_hash`, `embedding_status`, `embedding_updated_at`) — never carried across. A store whose embeddings turn out to be a non-semantic placeholder (all vectors produced by the same fixed scheme regardless of content) would poison vector search if imported as-is. New rows land with embeddings unset so the normal embed path repopulates them from real content.
- **`owner_id` / `scope`** — the current schema replaced this with `user_id`/`workspace_id`/`project_id`/`repository_id`/`task_id`/`session_id`, which has no equivalent to map from. Left unset rather than guessed.
- **Exact-duplicate content** — dropped by default, keeping only the earliest copy (`--no-dedupe-exact` to keep every copy).
- **Near-duplicate content** (same leading ~120 characters) — reported, but kept by default; a large fraction of an old store's content may look like this, so it isn't dropped silently. Pass `--dedupe-near` to opt into dropping later copies too.
- **Expired memories** — skipped by default (`expires_at` already in the past), counted separately in the report. Pass `--include-expired` to import them anyway.

### Recommended workflow

1. `recall-admin import-legacy --source <old db> --target-home <scratch dir> --dry-run` against a throwaway target first, and read the full plan.
2. Re-run without `--dry-run` against the same scratch target to confirm the numbers land as expected and the source file is unchanged (compare a checksum of the source before and after — it must never move).
3. Only then run the real import against the actual target home, ideally after taking a `recall-admin backup` of it.
4. Re-running the same command again afterward is safe and is expected to report zero new imports.

## MCP configuration

Canonical configuration:

~~~json
{
  "mcpServers": {
    "recall": {
      "command": "recall-mcp"
    }
  }
}
~~~

Existing configurations that invoke remembrance-mcp continue to launch. MCP tool names and REST routes are unchanged.

## Script migration

Use these canonical names:

- src/recall_mcp/integrations/claude_code/recall_mcp_stdio.cmd (packaged with
  `recall-mcp`; the old checkout-relative
  `integrations/claude-code/recall_mcp_stdio.cmd` now just delegates to it)
- integrations/windows/start_recall_rest.ps1
- integrations/windows/start_recall_rest.vbs

The previous filenames delegate to their canonical replacements and emit a warning where the host supports it.

## Git remote update

The remote repository has not been renamed automatically. After a maintainer renames it in GitHub, update each local checkout manually:

~~~bash
git remote set-url origin <NEW_REPOSITORY_URL>
git remote -v
~~~

Do not run the first command until the new URL is verified.

## Maintainer repository checklist

- [x] Rename the GitHub repository to recall
- Verify GitHub redirects
- Update repository description and topics
- Verify branch protections
- Update Actions environments and secrets
- Update webhook destinations
- Verify recall-mcp package-name availability
- Update package-registry metadata
- Update Docker registry paths
- Update absolute badges
- Update dependent repositories
- Update Prism references
- Update local remotes

## Compatibility-removal policy

The compatibility layer is temporary, but it must not be removed silently. Before removal:

1. Announce a target release and minimum deprecation window.
2. Keep release notes and this guide available.
3. Verify current integrations use Recall names.
4. Provide a command or documented procedure that detects legacy configuration.
5. Confirm users have migrated imports, commands, variables, and explicit paths.
6. Remove aliases only in a major release.

Historical changelogs may continue to use the former product name where accuracy requires it.