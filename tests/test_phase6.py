import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tests_helper  # noqa: F401  — auto-clean throwaway entities/projects on exit
import asyncio
from fastapi.testclient import TestClient
from main import app

def test_phase6():
    """
    Phase 6 — Env-based MCP identity.
    
    1. MCP server startup without KANBAN_AGENT_NAME fails immediately.
    2. MCP server resolves identity by KANBAN_AGENT_NAME → Entity.name.
    """
    with TestClient(app) as client:
        print("=== Phase 6: Resolve local human owner ===")
        human = tests_helper.get_local_owner(client)
        human_id = human["id"]
        print(f"Local human owner: id={human_id}")

        print("\n=== Phase 6: Verify adapters synced as entities ===")
        r = client.get("/entities?entity_type=agent")
        agents = r.json()
        print(f"Found {len(agents)} adapter entities")
        for a in agents:
            print(f"  - {a['name']}: role={a['role']}, active={a['is_active']}")

        # Find an active agent to use as mock caller
        active_agent = next((a for a in agents if a["is_active"]), None)
        assert active_agent, "Need at least one active adapter entity"
        agent_name = active_agent["name"]
        print(f"\nUsing active agent '{agent_name}' as mock MCP caller")

        print("\n=== Test 1: MCP server without KANBAN_AGENT_NAME fails ===")
        # Clear any existing env var
        for key in ["KANBAN_AGENT_NAME"]:
            os.environ.pop(key, None)

        try:
            from mcp_server import KanbanMCPServer
            # mcp library may not be installed; skip if unavailable
            server = KanbanMCPServer()
            print("FAIL: Server should not start without KANBAN_AGENT_NAME")
            assert False, "Expected RuntimeError"
        except RuntimeError as e:
            print(f"PASS: Got expected error: {e}")
        except ImportError:
            print("SKIP: MCP library not installed")

        print("\n=== Test 2: MCP server with KANBAN_AGENT_NAME resolves identity ===")
        os.environ["KANBAN_AGENT_NAME"] = agent_name
        os.environ["KANBAN_AGENT_ROLE"] = active_agent["role"]

        try:
            from mcp_server import KanbanMCPServer
            server = KanbanMCPServer()
            # Run async auth
            entity = asyncio.run(server._authenticate())
            print(f"PASS: Authenticated as {entity.name} (role={entity.role.value})")
            assert entity.name == agent_name, f"Expected name={agent_name}, got {entity.name}"
        except ImportError:
            print("SKIP: MCP library not installed")

        # Cleanup
        for key in ["KANBAN_AGENT_NAME", "KANBAN_AGENT_ROLE"]:
            os.environ.pop(key, None)

        print("\n=== Test 3: REST endpoints still work with X-Entity-ID ===")
        r = client.post("/projects", json={
            "name": "Phase 6 Test",
            "description": "Env-based identity test"
        }, headers={"x-entity-id": str(human_id)})
        assert r.status_code == 201, f"Expected 201, got {r.status_code}: {r.text}"
        print("PASS: Created project via X-Entity-ID")

        print("\n=== PHASE 6 TESTS PASSED ===")

if __name__ == "__main__":
    test_phase6()
