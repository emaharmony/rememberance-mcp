"""
MCP Server — Model Context Protocol Interface

PATTERN: Protocol Server (Adapter Pattern)
============================================

This module adapts our internal MemoryPipeline into the MCP protocol.
The MCP protocol is a standard that AI agents (Claude, GPT, etc.) use
to call tools on external systems.

HOW MCP WORKS:
  1. Client (Claude Desktop, Cursor, etc.) starts our server as a subprocess
  2. Client sends JSON-RPC messages over stdin/stdout
  3. Server responds with tool results
  4. When client disconnects, server shuts down

MESSAGE FORMAT (JSON-RPC 2.0):
  Request:  {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "memory_capture", "arguments": {...}}}
  Response: {"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": "..."}]}}

  This is the same format used by JSON-RPC APIs everywhere (Ethereum, etc.)

KEY CONCEPT: Tools vs Resources vs Prompts
  MCP defines three types of server capabilities:
  - Tools: Functions the AI can CALL (capture, search, consolidate)
  - Resources: Data the AI can READ (like files, but structured)
  - Prompts: Template messages the AI can use (not needed here)

  We only expose Tools because memory is action-oriented:
  you capture, you search, you consolidate.

WHY NOT REST API?
  REST requires a running server, port management, auth, CORS...
  MCP over stdio requires: nothing. Just launch the process.
  It's simpler, more secure (no network exposure), and faster (no HTTP overhead).
"""

import json
import logging

logger = logging.getLogger(__name__)


def create_server():
    """
    Create and configure the MCP server with all memory tools.

    Returns an MCP Server instance ready to run.
    """
    from mcp.server import Server
    from mcp.types import Tool, TextContent
    from recall_mcp.config import Settings
    from recall_mcp.pipeline import MemoryPipeline

    settings = Settings()
    pipeline = MemoryPipeline(settings=settings)

    server = Server(settings.MCP_SERVER_NAME)
    server.settings = settings  # type: ignore[attr-defined]
    server.recall_pipeline = pipeline  # type: ignore[attr-defined]

    def continuity_tool(
        name: str,
        description: str,
        required: list[str],
        properties: dict,
    ):
        return Tool(
            name=name,
            description=description,
            inputSchema={
                "type": "object",
                "properties": properties,
                "required": required,
            },
        )

    @server.list_tools()
    async def list_tools():
        """
        Called by the client to discover available tools.

        This is like an OpenAPI spec — it tells the AI what it can do,
        what parameters each tool takes, and what it returns.
        The AI uses this to decide which tool to call.
        """
        return [
            Tool(
                name="memory_capture",
                description=(
                    "Capture a piece of text as a memory. The system automatically "
                    "classifies its importance (skip/cold/active/persist), extracts "
                    "structured data (summary, category, topics), and stores it with "
                    "tier-based TTL. Use this for anything worth remembering: decisions, "
                    "project state, user preferences, important facts."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "maxLength": pipeline.settings.MAX_CAPTURE_CHARS,
                            "description": "The text to capture as a memory",
                        },
                        "source": {
                            "type": "string",
                            "description": "Where this came from (discord, cli, api, email, etc.)",
                            "default": "cli",
                        },
                        "category": {
                            "type": "string",
                            "enum": [
                                "project",
                                "person",
                                "preference",
                                "decision",
                                "task",
                                "strategy",
                                "session",
                            ],
                            "description": "Override auto-detected category (optional)",
                        },
                        "tier": {
                            "type": "string",
                            "enum": ["cold", "active", "persist"],
                            "description": "Override auto-detected tier (optional)",
                            "project": {
                                "type": "string",
                                "description": "Project scope (optional)",
                            },
                            "agent": {
                                "type": "string",
                                "description": "Agent scope (optional)",
                            },
                        },
                    },
                    "required": ["text"],
                },
            ),
            Tool(
                name="memory_search",
                description=(
                    "Search stored memories by keyword and metadata filters. "
                    "Returns matching memories sorted by recency."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query (keyword matching)",
                            "mode": {
                                "type": "string",
                                "enum": ["keyword", "vector", "balanced", "deep"],
                                "default": "balanced",
                                "minimum": 1,
                                "maximum": pipeline.settings.MAX_RESULTS,
                            },
                            "project": {
                                "type": "string",
                                "description": "Project scope (optional)",
                            },
                            "agent": {
                                "type": "string",
                                "description": "Agent scope (optional)",
                            },
                        },
                        "category": {
                            "type": "string",
                            "description": "Filter by category (optional)",
                        },
                        "tier": {
                            "type": "string",
                            "description": "Filter by tier (optional)",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results to return",
                            "default": 10,
                        },
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="memory_consolidate",
                description=(
                    "Run the decay/promotion cycle. Deletes expired cold memories, "
                    "promotes frequently-accessed active memories to persist, and "
                    "demotes rarely-accessed persist memories to active. Run this "
                    "periodically (e.g., daily) to keep the memory store healthy."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
            Tool(
                name="memory_get",
                description="Get a specific memory by its ID.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "The memory ID (e.g., mem_1234567890_abc123)",
                        },
                    },
                    "required": ["id"],
                },
            ),
            Tool(
                name="memory_delete",
                description="Delete a specific memory by its ID.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "The memory ID to delete",
                        },
                    },
                    "required": ["id"],
                },
            ),
            Tool(
                name="memory_metrics",
                description=(
                    "Get effectiveness metrics for the gate classifier. "
                    "Shows classification distribution, backend performance, "
                    "skip rate, fallback rate, and average confidence over the "
                    "specified time period. Use this to monitor and compare "
                    "gate backends (dilbert vs heuristic vs openai)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "hours": {
                            "type": "integer",
                            "description": "Look back period in hours (default: 24)",
                            "default": 24,
                        },
                    },
                },
            ),
            # ── V2 Tools ──────────────────────────────────────────────
            Tool(
                name="memory_graph_query",
                description=(
                    "Traverse the knowledge graph from an entity. Returns "
                    "connected entities and edges within N hops. Use this "
                    "to find relationships: 'Show me everything related to Prism'"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "entity": {
                            "type": "string",
                            "description": "Entity name or slug to start from",
                        },
                        "depth": {
                            "type": "integer",
                            "description": "Number of hops (default: 1)",
                            "default": 1,
                        },
                        "edge_types": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Filter by edge types (optional)",
                        },
                    },
                    "required": ["entity"],
                },
            ),
            Tool(
                name="memory_entity_get",
                description=(
                    "Get an entity's compiled truth and timeline. Returns "
                    "the always-current synthesis of what we know about "
                    "a person, project, concept, etc."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Entity name or slug",
                        },
                    },
                    "required": ["name"],
                },
            ),
            Tool(
                name="memory_entity_search",
                description=(
                    "Search entities by name or type. Returns matching "
                    "entities with their compiled truth."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query (entity name)",
                        },
                        "entity_type": {
                            "type": "string",
                            "description": "Filter by type: person, project, concept, tool, decision, preference",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results (default: 10)",
                            "default": 10,
                        },
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="memory_dream",
                description=(
                    "Trigger the dream cycle manually. Runs maintenance "
                    "phases: entity sweep, backlink audit, truth re-synthesis, "
                    "pattern detection, orphan detection, and purge."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "phases": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Specific phases to run (default: all)",
                        },
                        "dry_run": {
                            "type": "boolean",
                            "description": "Report without making changes (default: false)",
                            "default": False,
                        },
                    },
                },
            ),
            Tool(
                name="memory_context_build",
                description=(
                    "Build context for a task. Returns relevant memories, "
                    "entities, and open threads. This is what agents call "
                    "before working on a task to load relevant context."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": "Task description to find context for",
                        },
                        "project": {
                            "type": "string",
                            "description": "Project name (optional)",
                        },
                        "agent": {
                            "type": "string",
                            "description": "Agent name (optional)",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max memories to return (default: 10)",
                            "default": 10,
                        },
                        "user_id": {"type": "string"},
                        "workspace_id": {"type": "string"},
                        "project_id": {"type": "string"},
                        "repository_id": {"type": "string"},
                        "task_id": {"type": "string"},
                        "session_id": {"type": "string"},
                        "agent_id": {"type": "string"},
                        "known_checkpoint_version": {
                            "type": "integer",
                            "minimum": 0,
                        },
                    },
                    "anyOf": [{"required": ["task"]}, {"required": ["task_id"]}],
                },
            ),
            continuity_tool(
                "recall_task_create",
                "Create an agent-neutral task in a validated repository scope.",
                [
                    "user_id",
                    "workspace_id",
                    "project_id",
                    "repository_id",
                    "title",
                    "objective",
                    "agent_id",
                ],
                {
                    "user_id": {"type": "string"},
                    "workspace_id": {"type": "string"},
                    "project_id": {"type": "string"},
                    "repository_id": {"type": "string"},
                    "title": {"type": "string"},
                    "objective": {"type": "string"},
                    "agent_id": {"type": "string"},
                    "agent_system_type": {"type": "string", "default": "other"},
                    "status": {"type": "string", "default": "active"},
                    "idempotency_key": {"type": "string"},
                    "canonical_path": {"type": "string"},
                    "remote_url": {"type": "string"},
                    "default_branch": {"type": "string"},
                },
            ),
            continuity_tool(
                "recall_task_get",
                "Read a task and optionally verify its scope.",
                ["task_id"],
                {
                    "task_id": {"type": "string"},
                    "user_id": {"type": "string"},
                    "project_id": {"type": "string"},
                    "repository_id": {"type": "string"},
                },
            ),
            continuity_tool(
                "recall_task_update",
                "Update task title, objective, or lifecycle status.",
                ["task_id"],
                {
                    "task_id": {"type": "string"},
                    "title": {"type": "string"},
                    "objective": {"type": "string"},
                    "status": {"type": "string"},
                },
            ),
            continuity_tool(
                "recall_session_start",
                "Start a shared task session and join its initiating agent.",
                ["task_id", "agent_id"],
                {
                    "task_id": {"type": "string"},
                    "agent_id": {"type": "string"},
                    "role": {"type": "string", "default": "owner"},
                    "agent_system_type": {"type": "string", "default": "other"},
                    "idempotency_key": {"type": "string"},
                },
            ),
            continuity_tool(
                "recall_session_join",
                "Join another agent to an existing shared session.",
                ["session_id", "agent_id"],
                {
                    "session_id": {"type": "string"},
                    "agent_id": {"type": "string"},
                    "role": {"type": "string", "default": "implementer"},
                    "agent_system_type": {"type": "string", "default": "other"},
                    "idempotency_key": {"type": "string"},
                },
            ),
            continuity_tool(
                "recall_session_event",
                "Append one ordered, attributed event to a session.",
                ["session_id", "agent_id", "event_type"],
                {
                    "session_id": {"type": "string"},
                    "agent_id": {"type": "string"},
                    "event_type": {"type": "string"},
                    "payload": {"type": "object"},
                    "idempotency_key": {"type": "string"},
                },
            ),
            continuity_tool(
                "recall_session_checkpoint",
                "Create a deterministic structured session checkpoint.",
                ["session_id", "agent_id"],
                {
                    "session_id": {"type": "string"},
                    "agent_id": {"type": "string"},
                    "summary": {"type": "string"},
                    "completed": {"type": "array"},
                    "remaining": {"type": "array"},
                    "constraints": {"type": "array"},
                    "approved_decisions": {"type": "array"},
                    "proposed_decisions": {"type": "array"},
                    "open_questions": {"type": "array"},
                    "blockers": {"type": "array"},
                    "important_files": {"type": "array"},
                    "known_failures": {"type": "array"},
                    "idempotency_key": {"type": "string"},
                },
            ),
            continuity_tool(
                "recall_session_delta",
                "Return ordered session changes after a known checkpoint version.",
                ["session_id", "known_version"],
                {
                    "session_id": {"type": "string"},
                    "known_version": {"type": "integer", "minimum": 0},
                    "agent_id": {"type": "string"},
                },
            ),
            continuity_tool(
                "recall_session_close",
                "Close a session without deleting its events or checkpoints.",
                ["session_id", "agent_id"],
                {
                    "session_id": {"type": "string"},
                    "agent_id": {"type": "string"},
                    "summary": {"type": "string"},
                    "idempotency_key": {"type": "string"},
                },
            ),
            continuity_tool(
                "recall_context_feedback",
                "Record attributed retrieval and context usage feedback.",
                ["context_pack_id"],
                {
                    "context_pack_id": {"type": "string"},
                    "agent_id": {"type": "string"},
                    "used_memory_ids": {"type": "array"},
                    "ignored_memory_ids": {"type": "array"},
                    "expanded_memory_ids": {"type": "array"},
                    "corrected_memory_ids": {"type": "array"},
                    "rejected_memory_ids": {"type": "array"},
                    "idempotency_key": {"type": "string"},
                },
            ),
            continuity_tool(
                "recall_task_outcome",
                "Record a task outcome for context the task actually used.",
                ["task_id", "status", "successful"],
                {
                    "task_id": {"type": "string"},
                    "session_id": {"type": "string"},
                    "status": {"type": "string"},
                    "successful": {"type": "boolean"},
                    "agent_id": {"type": "string"},
                    "user_correction_count": {"type": "integer", "minimum": 0},
                    "rework_required": {"type": "boolean"},
                    "metadata": {"type": "object"},
                    "idempotency_key": {"type": "string"},
                },
            ),
            continuity_tool(
                "recall_memory_utility",
                "Explain a memory's versioned utility components.",
                ["memory_id"],
                {
                    "memory_id": {"type": "string"},
                    "user_id": {"type": "string"},
                    "project_id": {"type": "string"},
                    "repository_id": {"type": "string"},
                    "task_id": {"type": "string"},
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        """
        Called by the client when the AI decides to use a tool.

        This is the "router" — it dispatches to the right pipeline method
        based on the tool name the AI selected.
        """
        try:
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be an object")

            def result_json(result: object):
                return [
                    TextContent(
                        type="text", text=json.dumps(result, indent=2, default=str)
                    )
                ]

            if name == "recall_task_create":
                result = pipeline.task_service.create_task(
                    user_id=arguments["user_id"],
                    workspace_id=arguments["workspace_id"],
                    project_id=arguments["project_id"],
                    repository_id=arguments["repository_id"],
                    title=arguments["title"],
                    objective=arguments["objective"],
                    created_by=arguments["agent_id"],
                    status=arguments.get("status", "active"),
                    idempotency_key=arguments.get("idempotency_key"),
                    agent_system_type=arguments.get("agent_system_type", "other"),
                    canonical_path=arguments.get("canonical_path"),
                    remote_url=arguments.get("remote_url"),
                    default_branch=arguments.get("default_branch"),
                )
                return result_json(result)

            elif name == "recall_task_get":
                return result_json(
                    pipeline.task_service.get_task(
                        arguments["task_id"],
                        user_id=arguments.get("user_id"),
                        project_id=arguments.get("project_id"),
                        repository_id=arguments.get("repository_id"),
                    )
                )

            elif name == "recall_task_update":
                return result_json(
                    pipeline.task_service.update_task(
                        arguments["task_id"],
                        title=arguments.get("title"),
                        objective=arguments.get("objective"),
                        status=arguments.get("status"),
                    )
                )

            elif name == "recall_session_start":
                return result_json(
                    pipeline.session_service.start_session(
                        task_id=arguments["task_id"],
                        agent_id=arguments["agent_id"],
                        role=arguments.get("role", "owner"),
                        idempotency_key=arguments.get("idempotency_key"),
                        agent_system_type=arguments.get("agent_system_type", "other"),
                    )
                )

            elif name == "recall_session_join":
                return result_json(
                    pipeline.session_service.join_session(
                        arguments["session_id"],
                        agent_id=arguments["agent_id"],
                        role=arguments.get("role", "implementer"),
                        idempotency_key=arguments.get("idempotency_key"),
                        agent_system_type=arguments.get("agent_system_type", "other"),
                    )
                )

            elif name == "recall_session_event":
                return result_json(
                    pipeline.session_service.append_event(
                        arguments["session_id"],
                        agent_id=arguments["agent_id"],
                        event_type=arguments["event_type"],
                        payload=arguments.get("payload"),
                        idempotency_key=arguments.get("idempotency_key"),
                    )
                )

            elif name == "recall_session_checkpoint":
                return result_json(
                    pipeline.session_service.create_checkpoint(
                        arguments["session_id"],
                        agent_id=arguments["agent_id"],
                        summary=arguments.get("summary", ""),
                        completed=arguments.get("completed"),
                        remaining=arguments.get("remaining"),
                        constraints=arguments.get("constraints"),
                        approved_decisions=arguments.get("approved_decisions"),
                        proposed_decisions=arguments.get("proposed_decisions"),
                        open_questions=arguments.get("open_questions"),
                        blockers=arguments.get("blockers"),
                        important_files=arguments.get("important_files"),
                        known_failures=arguments.get("known_failures"),
                        idempotency_key=arguments.get("idempotency_key"),
                    )
                )

            elif name == "recall_session_delta":
                return result_json(
                    pipeline.session_service.get_delta(
                        arguments["session_id"],
                        known_version=int(arguments["known_version"]),
                        agent_id=arguments.get("agent_id"),
                    )
                )

            elif name == "recall_session_close":
                return result_json(
                    pipeline.session_service.close_session(
                        arguments["session_id"],
                        agent_id=arguments["agent_id"],
                        summary=arguments.get("summary"),
                        idempotency_key=arguments.get("idempotency_key"),
                    )
                )

            elif name == "recall_context_feedback":
                return result_json(
                    pipeline.feedback_service.record_feedback(
                        context_pack_id=arguments["context_pack_id"],
                        agent_id=arguments.get("agent_id"),
                        used_memory_ids=arguments.get("used_memory_ids", []),
                        ignored_memory_ids=arguments.get("ignored_memory_ids", []),
                        expanded_memory_ids=arguments.get("expanded_memory_ids", []),
                        corrected_memory_ids=arguments.get("corrected_memory_ids", []),
                        rejected_memory_ids=arguments.get("rejected_memory_ids", []),
                        idempotency_key=arguments.get("idempotency_key"),
                    )
                )

            elif name == "recall_task_outcome":
                return result_json(
                    pipeline.feedback_service.record_task_outcome(
                        task_id=arguments["task_id"],
                        session_id=arguments.get("session_id"),
                        status=arguments["status"],
                        successful=bool(arguments["successful"]),
                        agent_id=arguments.get("agent_id"),
                        user_correction_count=int(
                            arguments.get("user_correction_count", 0)
                        ),
                        rework_required=bool(arguments.get("rework_required", False)),
                        metadata=arguments.get("metadata"),
                        idempotency_key=arguments.get("idempotency_key"),
                    )
                )

            elif name == "recall_memory_utility":
                scope = {
                    key: arguments[key]
                    for key in ("user_id", "project_id", "repository_id", "task_id")
                    if arguments.get(key) is not None
                }
                return result_json(
                    pipeline.feedback_service.explain_utility(
                        arguments["memory_id"], scope=scope
                    )
                )

            elif name == "memory_capture":
                text = arguments["text"]
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("text must be a non-empty string")
                if len(text) > pipeline.settings.MAX_CAPTURE_CHARS:
                    raise ValueError("capture exceeds the configured character limit")
                result = pipeline.capture(
                    text=text,
                    source=arguments.get("source", "cli"),
                    category=arguments.get("category"),
                    tier=arguments.get("tier"),
                    project=arguments.get("project"),
                    agent=arguments.get("agent"),
                    user_id=arguments.get("user_id"),
                    workspace_id=arguments.get("workspace_id"),
                    project_id=arguments.get("project_id"),
                    repository_id=arguments.get("repository_id"),
                    task_id=arguments.get("task_id"),
                    session_id=arguments.get("session_id"),
                )
                if result["decision"] == "SKIP":
                    return [
                        TextContent(
                            type="text", text="Skipped — not important enough to store."
                        )
                    ]
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(result, indent=2),
                    )
                ]

            elif name == "memory_search":
                results = pipeline.search(
                    query=arguments["query"],
                    category=arguments.get("category"),
                    tier=arguments.get("tier"),
                    limit=min(
                        int(arguments.get("limit", 10)),
                        pipeline.settings.MAX_RESULTS,
                    ),
                    mode=arguments.get("mode", "balanced"),
                    project=arguments.get("project"),
                    agent=arguments.get("agent"),
                )
                if not results:
                    return [
                        TextContent(
                            type="text", text="No memories found matching that query."
                        )
                    ]
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(results, indent=2, default=str),
                    )
                ]

            elif name == "memory_consolidate":
                result = pipeline.consolidate()
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(result, indent=2),
                    )
                ]

            elif name == "memory_get":
                memory = pipeline.get(arguments["id"])
                if not memory:
                    return [
                        TextContent(
                            type="text", text=f"Memory {arguments['id']} not found."
                        )
                    ]
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(memory, indent=2, default=str),
                    )
                ]

            elif name == "memory_delete":
                deleted = pipeline.delete(arguments["id"])
                if deleted:
                    return [
                        TextContent(
                            type="text", text=f"Deleted memory {arguments['id']}."
                        )
                    ]
                return [
                    TextContent(
                        type="text", text=f"Memory {arguments['id']} not found."
                    )
                ]

            elif name == "memory_metrics":
                metrics = pipeline.metrics_summary(hours=arguments.get("hours", 24))
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(metrics, indent=2),
                    )
                ]

            # ── V2 Tool Handlers ──────────────────────────────────────────
            elif name == "memory_graph_query":
                entity_name = arguments["entity"]
                depth = max(
                    1,
                    min(
                        int(arguments.get("depth", 1)),
                        pipeline.settings.MAX_GRAPH_DEPTH,
                    ),
                )
                edge_types = arguments.get("edge_types")
                # Find entity
                entity = pipeline.entity_store.find_entity(entity_name)
                if not entity:
                    return [
                        TextContent(
                            type="text", text=f"Entity '{entity_name}' not found."
                        )
                    ]
                # Traverse
                result = pipeline.entity_store.get_neighbors(
                    entity["id"], depth=depth, edge_types=edge_types
                )
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(result, indent=2, default=str),
                    )
                ]

            elif name == "memory_entity_get":
                entity_name = arguments["name"]
                entity = pipeline.entity_get(entity_name)
                if not entity:
                    return [
                        TextContent(
                            type="text", text=f"Entity '{entity_name}' not found."
                        )
                    ]
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(entity, indent=2, default=str),
                    )
                ]

            elif name == "memory_entity_search":
                results = pipeline.entity_store.search_entities(
                    query=arguments["query"],
                    entity_type=arguments.get("entity_type"),
                    limit=max(
                        1,
                        min(
                            int(arguments.get("limit", 10)),
                            pipeline.settings.MAX_RESULTS,
                        ),
                    ),
                )
                if not results:
                    return [TextContent(type="text", text="No entities found.")]
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(results, indent=2, default=str),
                    )
                ]

            elif name == "memory_dream":
                phases = arguments.get("phases")
                dry_run = arguments.get("dry_run", False)
                result = pipeline.dream_cycle.run(phases=phases, dry_run=dry_run)
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(result, indent=2, default=str),
                    )
                ]

            elif name == "memory_context_build":
                result = pipeline.build_context(
                    task=arguments.get("task", ""),
                    project=arguments.get("project"),
                    agent=arguments.get("agent"),
                    limit=max(
                        1,
                        min(
                            int(arguments.get("limit", 10)),
                            pipeline.settings.MAX_RESULTS,
                        ),
                    ),
                    user_id=arguments.get("user_id"),
                    workspace_id=arguments.get("workspace_id"),
                    project_id=arguments.get("project_id"),
                    repository_id=arguments.get("repository_id"),
                    task_id=arguments.get("task_id"),
                    session_id=arguments.get("session_id"),
                    agent_id=arguments.get("agent_id"),
                    known_checkpoint_version=arguments.get("known_checkpoint_version"),
                    token_budget=arguments.get("token_budget"),
                    include_cold=bool(arguments.get("include_cold", False)),
                    retrieval_idempotency_key=arguments.get("idempotency_key"),
                )
                if result.get("context_pack_id"):
                    pipeline.feedback_service.mark_context_injected(
                        str(result["context_pack_id"]),
                        agent_id=arguments.get("agent_id") or arguments.get("agent"),
                    )
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(result, indent=2, default=str),
                    )
                ]

            else:
                return [TextContent(type="text", text=f"Unknown tool: {name}")]

        except (KeyError, TypeError, ValueError) as exc:
            logger.info("Invalid tool request for %s: %s", name, exc)
            return [TextContent(type="text", text=f"Invalid request: {exc}")]
        except Exception:
            logger.exception("Tool call failed for %s", name)
            return [
                TextContent(
                    type="text",
                    text="Error: request could not be completed; consult server logs.",
                )
            ]

    return server
