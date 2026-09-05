"""The MCP server must work on both mcp generations.

mcp 1.x registers tool handlers with `@server.list_tools()` /
`@server.call_tool()`. 2.x removed those decorators in favour of
`add_request_handler(method, params_type, handler)`, where the handler takes a
request context plus parsed params and returns a Result model rather than a
bare list.

The dependency is unpinned, so whichever generation a user's environment
resolves has to work. These tests assert the registration actually happened on
the installed generation and that the tool definitions survive it — pinning to
`<2` was the previous workaround and it collided with anything needing 2.x.
"""

from __future__ import annotations

import os

import pytest

import tests_helper  # noqa: F401  — autouse cleanup listeners

from agent_kanban_pm.mcp.server import (
    MCP_AVAILABLE,
    MCP_LEGACY_DECORATORS,
    KanbanMCPServer,
)

pytestmark = pytest.mark.skipif(
    not MCP_AVAILABLE, reason="mcp library not installed"
)


@pytest.fixture
def server(monkeypatch):
    """A constructed server, which is what runs setup_handlers()."""
    monkeypatch.setenv("KANBAN_AGENT_NAME", "claude")
    monkeypatch.setenv("KANBAN_AGENT_ROLE", "worker")
    return KanbanMCPServer()


def _list_tools_handler(server):
    """Return a zero-argument coroutine factory for the tools/list handler."""
    import mcp.types as types

    if MCP_LEGACY_DECORATORS:
        handler = server.server.request_handlers[types.ListToolsRequest]

        async def call():
            result = await handler(types.ListToolsRequest(method="tools/list"))
            return result.root.tools

        return call

    entry = server.server.get_request_handler("tools/list")
    handler = getattr(entry, "handler", None) or entry[1]

    async def call():
        result = await handler(None, None)
        assert isinstance(result, types.ListToolsResult), type(result)
        return result.tools

    return call


def test_server_constructs_on_the_installed_mcp_generation(server):
    """Construction runs setup_handlers, which is where the API differs."""
    assert server.server is not None


def test_tool_handlers_are_registered(server):
    import mcp.types as types

    if MCP_LEGACY_DECORATORS:
        registered = set(server.server.request_handlers)
        assert types.ListToolsRequest in registered
        assert types.CallToolRequest in registered
    else:
        assert server.server.get_request_handler("tools/list") is not None
        assert server.server.get_request_handler("tools/call") is not None


@pytest.mark.asyncio
async def test_list_tools_returns_the_full_tool_set(server):
    tools = await _list_tools_handler(server)()
    names = {tool.name for tool in tools}
    assert len(names) > 20, f"suspiciously few tools registered: {sorted(names)}"
    # A representative spread across the permission tiers.
    for expected in ("create_project", "claim_task", "add_comment", "assign_task"):
        assert expected in names, f"{expected} missing from {sorted(names)}"


@pytest.mark.asyncio
async def test_every_tool_keeps_its_input_schema(server):
    """2.x renamed inputSchema to input_schema, keeping the old name as an alias.

    Reading through the wire alias catches both a dropped schema and a rename
    that silently stopped populating it.
    """
    tools = await _list_tools_handler(server)()
    for tool in tools:
        wire = tool.model_dump(by_alias=True)
        schema = wire.get("inputSchema")
        assert schema, f"{tool.name} lost its input schema"
        assert schema.get("type") == "object", f"{tool.name}: {schema!r}"


def test_legacy_flag_matches_the_installed_library():
    """The flag drives registration, so a wrong value silently registers nothing."""
    from mcp.server import Server

    assert MCP_LEGACY_DECORATORS == hasattr(Server, "list_tools")


def test_pyproject_does_not_pin_mcp_below_2():
    """The <2 pin was the old workaround; it must not creep back."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():  # installed-package test run
        pytest.skip("pyproject.toml not present in this layout")
    text = pyproject.read_text(encoding="utf-8")
    assert "mcp>=1.0,<2" not in text, "mcp is pinned below 2.x again"
    assert '"mcp>=1.0"' in text
