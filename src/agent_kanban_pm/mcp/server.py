#!/usr/bin/env python3
"""
MCP Server for Agent Kanban PM

Provides Model Context Protocol tools for AI agents to interact with
the Kanban board. Designed for ephemeral CLI agents (Claude Code, Codex,
OpenCode, Antigravity) that connect via stdio.

Because MCP stdio servers cannot push events, agents must poll for updates
using the `get_pending_events` tool.

Usage:
    python -m agent_kanban_pm.mcp.server

The MCP server communicates over stdin/stdout with the host AI tool.
"""

import asyncio
import json
import logging
import sys
from typing import Any, Optional, Sequence
from sqlalchemy import select

from agent_kanban_pm.db import async_session_maker
from agent_kanban_pm.models import (
    Entity, Role
)
from agent_kanban_pm.auth import ROLE_LEVELS, get_effective_role
from agent_kanban_pm.mcp.handlers.agents import AgentHandlers
from agent_kanban_pm.mcp.handlers.approvals import ApprovalHandlers
from agent_kanban_pm.mcp.handlers.coordination import CoordinationHandlers
from agent_kanban_pm.mcp.handlers.projects import ProjectHandlers
from agent_kanban_pm.mcp.handlers.reviews import ReviewHandlers
from agent_kanban_pm.mcp.handlers.sessions import SessionHandlers
from agent_kanban_pm.mcp.handlers.stages import StageHandlers
from agent_kanban_pm.mcp.handlers.tasks import TaskHandlers
from agent_kanban_pm.mcp.tool_schemas import build_tools

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)]
)
logger = logging.getLogger(__name__)

try:
    import mcp.types as mcp_types
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import (
        Tool,
        TextContent,
    )
    MCP_AVAILABLE = True
    # mcp 1.x registers handlers with @server.list_tools() / @server.call_tool();
    # 2.x dropped those decorators for explicit add_request_handler() calls.
    # Everything else this module uses (Server.run, stdio_server, the types) is
    # the same on both, so one flag covers the difference.
    MCP_LEGACY_DECORATORS = hasattr(Server, "list_tools")
except ImportError as e:
    MCP_AVAILABLE = False
    MCP_LEGACY_DECORATORS = False
    logger.error(f"MCP library not available: {e}")
    logger.error("Install with: pip install mcp")


class KanbanMCPServer(
    ProjectHandlers,
    TaskHandlers,
    AgentHandlers,
    SessionHandlers,
    CoordinationHandlers,
    ReviewHandlers,
    ApprovalHandlers,
    StageHandlers,
):
    """MCP Server for Agent Kanban PM system.

    Identity, authentication and MCP registration live here; the ``_handle_*``
    tool implementations live in the ``handlers`` mixins and the tool schemas in
    ``tool_schemas``.
    """

    def __init__(self):
        if not MCP_AVAILABLE:
            raise RuntimeError("MCP library not installed")
        self.server = Server("agent-kanban-pm")
        self.caller_entity: Optional[Entity] = None
        self._caller_name: Optional[str] = None
        self._caller_role: str = "worker"
        self._resolve_caller()
        self.setup_handlers()

    def _resolve_caller(self):
        """
        Resolve caller identity from KANBAN_AGENT_NAME env var.
        Local-first: the OS process is the trust boundary.
        """
        import os
        name = os.environ.get("KANBAN_AGENT_NAME")
        if not name:
            logger.error("KANBAN_AGENT_NAME not set in environment. MCP identity required.")
            raise RuntimeError("KANBAN_AGENT_NAME not set in environment")
        self._caller_name = name
        self._caller_role = os.environ.get("KANBAN_AGENT_ROLE", "worker")

    async def _authenticate(self) -> Entity:
        """Authenticate the caller against current database state.

        MCP processes are intentionally long-lived, while an entity can be
        disabled or have its role changed at any time. Re-read the row for
        every tool call so those changes take effect immediately.
        """
        async with async_session_maker() as db:
            result = await db.execute(
                select(Entity).filter(Entity.name == self._caller_name, Entity.is_active == True)
            )
            entity = result.scalar_one_or_none()
            if not entity:
                logger.error(f"No active entity named '{self._caller_name}'")
                raise RuntimeError(f"MCP identity '{self._caller_name}' not found")
            self.caller_entity = entity
            logger.info(f"MCP authenticated as {entity.name} (role={entity.role.value})")
            return entity

    def _require_role(self, min_role: Role):
        """Check if the authenticated caller has at least the required role."""
        entity = self.caller_entity
        if not entity:
            raise PermissionError("Not authenticated")
        effective = get_effective_role(entity)
        if ROLE_LEVELS.get(effective, 0) < ROLE_LEVELS.get(min_role, 0):
            raise PermissionError(f"Insufficient permissions. Required: {min_role.value}, have: {effective.value}")

    def _target_agent_id(self, args: dict) -> int:
        """Return the allowed agent target for a tool call."""
        requested = args.get("agent_id")
        if requested is None or requested == self.caller_entity.id:
            return self.caller_entity.id
        self._require_role(Role.MANAGER)
        return requested

    def setup_handlers(self):
        """Setup MCP handlers"""

        async def list_tools() -> list[Tool]:
            """List available MCP tools for agents"""
            return build_tools(Tool)

        async def call_tool(name: str, arguments: Any) -> Sequence[TextContent]:
            """Handle tool calls from agents"""
            try:
                # Authenticate on first tool call
                await self._authenticate()
                handler = getattr(self, f"_handle_{name}", None)
                if handler:
                    result = await handler(arguments or {})
                    return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]
                else:
                    return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]
            except PermissionError as e:
                logger.warning(f"Permission denied for tool {name}: {e}")
                return [TextContent(type="text", text=json.dumps({"error": f"Permission denied: {e}"}))]
            except Exception as e:
                logger.error(f"Error in tool {name}: {e}")
                return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

        self._register_tool_handlers(list_tools, call_tool)

    def _register_tool_handlers(self, list_tools, call_tool) -> None:
        """Attach the tool handlers to whichever mcp generation is installed.

        1.x wraps them with @server.list_tools() / @server.call_tool(), which
        adapt the return values themselves. 2.x removed those decorators: the
        handlers are registered by method name, take a request context plus
        parsed params, and must return the Result models rather than bare
        lists.
        """
        if MCP_LEGACY_DECORATORS:
            self.server.list_tools()(list_tools)
            self.server.call_tool()(call_tool)
            return

        async def on_list_tools(_ctx, _params):
            return mcp_types.ListToolsResult(tools=list(await list_tools()))

        async def on_call_tool(_ctx, params):
            content = await call_tool(params.name, params.arguments or {})
            return mcp_types.CallToolResult(content=list(content))

        self.server.add_request_handler(
            "tools/list", mcp_types.PaginatedRequestParams, on_list_tools
        )
        self.server.add_request_handler(
            "tools/call", mcp_types.CallToolRequestParams, on_call_tool
        )

    async def run(self):
        """Run the MCP server"""
        async with stdio_server() as (read_stream, write_stream):
            await self.server.run(read_stream, write_stream, self.server.create_initialization_options())


async def main():
    """Main entry point"""
    if not MCP_AVAILABLE:
        print("Error: MCP library not available", file=sys.stderr)
        print("Install with: pip install mcp", file=sys.stderr)
        return 1

    server = KanbanMCPServer()
    await server.run()


def cli_main() -> int:
    """Synchronous console-script entry point for kanban-mcp."""
    result = asyncio.run(main())
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(cli_main())
