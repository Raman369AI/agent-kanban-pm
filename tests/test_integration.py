import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tests_helper  # noqa: F401  — auto-clean throwaway entities/projects on exit
from fastapi.testclient import TestClient
from main import app

def test_integration():
    with TestClient(app) as client:
        print("=== 1. Resolve local human owner ===")
        human = tests_helper.get_local_owner(client)
        human_id = human["id"]
        assert human["role"] == "owner"

        print("=== 2. Adapters auto-synced as entities ===")
        r = client.get("/entities?entity_type=agent")
        agents = r.json()
        # Some adapters may be inactive if CLI tool is not installed
        active_agents = [a for a in agents if a.get("is_active", True)]
        assert len(agents) >= 3, f"Expected at least 3 adapter entities, got {len(agents)}"
        print(f"Found {len(agents)} adapter entities ({len(active_agents)} active)")

        print("=== 3. Create project ===")
        r = client.post("/projects", json={
            "name": "Integration Test",
            "description": "Full stack test"
        }, headers={"x-entity-id": str(human_id)})
        project = r.json()
        project_id = project["id"]
        assert project["approval_status"] == "PENDING"

        print("=== 4. Approve project ===")
        r = client.post(f"/projects/{project_id}/approve", headers={"x-entity-id": str(human_id)})
        assert r.json()["approval_status"] == "APPROVED"

        print("=== 5. Create task ===")
        r = client.post("/tasks", json={
            "title": "Test Task",
            "project_id": project_id
        }, headers={"x-entity-id": str(human_id)})
        task = r.json()
        task_id = task["id"]
        assert task["created_by"] == human_id

        print("=== 6. Agent heartbeat ===")
        agent = agents[0]
        agent_headers = {"x-entity-id": str(agent["id"])}
        r = client.post(f"/agents/{agent['id']}/status", json={
            "status_type": "working",
            "message": "On it",
            "task_id": task_id
        }, headers=agent_headers)
        assert r.status_code == 200

        print("=== 7. Get agent statuses ===")
        r = client.get("/agents/status")
        status = next(s for s in r.json() if s["agent_id"] == agent["id"])
        assert status["status_type"] == "working"

        print("=== 8. Log activity ===")
        r = client.post(f"/agents/{agent['id']}/activity", json={
            "message": "Analyzed the task",
            "activity_type": "thought",
            "task_id": task_id
        }, headers=agent_headers)
        assert r.status_code == 201

        print("=== 9. Get activity feed ===")
        r = client.get(f"/agents/activity?agent_id={agent['id']}")
        assert any(a["activity_type"] == "thought" for a in r.json())

        print("=== 10. Settings reflect manager mode ===")
        r = client.get("/ui/api/settings")
        settings = r.json()
        print(f"Settings: {settings}")
        # Since no preferences.yaml was created, manager is None
        assert "manager" in settings

        print("\n=== ALL INTEGRATION TESTS PASSED ===")

if __name__ == "__main__":
    test_integration()
