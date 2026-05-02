import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tests_helper  # noqa: F401  — auto-clean throwaway entities/projects on exit
from fastapi.testclient import TestClient
from main import app

def test_phase1():
    with TestClient(app) as client:
        print("=== Resolve local owner ===")
        owner = tests_helper.get_local_owner(client)
        owner_id = owner["id"]

        print("=== Register agent ===")
        r = client.post("/entities/register/agent", json={
            "name": "Heartbeat Agent",
            "entity_type": "agent"
        }, headers={"x-entity-id": str(owner_id)})
        print(f"Status: {r.status_code}")
        agent = r.json()
        agent_id = agent["id"]
        headers = {"x-entity-id": str(agent_id)}

        print("\n=== Report status via REST ===")
        r = client.post(f"/agents/{agent_id}/status", json={
            "status_type": "working",
            "message": "Starting task analysis",
            "task_id": 1
        }, headers=headers)
        print(f"Status: {r.status_code}, Body: {r.json()}")
        assert r.status_code == 200

        print("\n=== Get all agent statuses ===")
        r = client.get("/agents/status")
        print(f"Status: {r.status_code}, Body: {r.json()}")
        status = next(s for s in r.json() if s["agent_id"] == agent_id)
        assert status["status_type"] == "working"

        print("\n=== Log activity ===")
        r = client.post(f"/agents/{agent_id}/activity", json={
            "message": "Analyzed requirements",
            "activity_type": "thought",
            "task_id": 1
        }, headers=headers)
        print(f"Status: {r.status_code}, Body: {r.json()}")
        assert r.status_code == 201

        print("\n=== Get activity feed ===")
        r = client.get(f"/agents/activity?agent_id={agent_id}")
        print(f"Status: {r.status_code}, Body: {r.json()}")
        assert any(a["activity_type"] == "thought" for a in r.json())

        print("\n=== Update status again ===")
        r = client.post(f"/agents/{agent_id}/status", json={
            "status_type": "done",
            "message": "Task completed"
        }, headers=headers)
        print(f"Status: {r.status_code}, Body: {r.json()}")
        assert r.status_code == 200

        print("\n=== Verify status updated (still only 1 row) ===")
        r = client.get("/agents/status")
        print(f"Status: {r.status_code}, Body: {r.json()}")
        status = next(s for s in r.json() if s["agent_id"] == agent_id)
        assert status["status_type"] == "done"

        print("\n=== PHASE 1 TESTS PASSED ===")

if __name__ == "__main__":
    test_phase1()
