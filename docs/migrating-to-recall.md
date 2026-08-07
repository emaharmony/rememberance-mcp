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

- integrations/claude-code/recall_mcp_stdio.cmd
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