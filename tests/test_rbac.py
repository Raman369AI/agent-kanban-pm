import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tests_helper  # noqa: F401  — auto-clean throwaway entities/projects on exit
from fastapi.testclient import TestClient
from main import app

def test_rbac():
    suffix = uuid.uuid4().hex[:8]
    with TestClient(app) as client:
        print("=== Test 1: Resolve local human owner ===")
        admin = tests_helper.get_local_owner(client)
        print(f"Owner: {admin}")
        admin_id = admin["id"]

        print("\n=== Test 2: Register agent (should be WORKER) ===")
        r = client.post("/entities/register/agent", json={
            "name": f"Worker Agent {suffix}",
            "entity_type": "agent",
            "skills": "python,testing"
        }, headers={"x-entity-id": str(admin_id)})
        print(f"Status: {r.status_code}, Body: {r.json()}")
        agent = r.json()
        agent_id = agent["id"]

        print("\n=== Test 3: Create project as admin ===")
        r = client.post("/projects", json={
            "name": "Test Project",
            "description": "RBAC test"
        }, headers={"x-entity-id": str(admin_id)})
        print(f"Status: {r.status_code}, Body: {r.json()}")
        project = r.json()
        project_id = project["id"]
        assert project["approval_status"] == "PENDING", "Project should start as PENDING"

        print("\n=== Test 4: Agent tries to create task in PENDING project (should fail) ===")
        r = client.post("/tasks", json={
            "title": "Agent Task",
            "project_id": project_id
        }, headers={"x-entity-id": str(agent_id)})
        print(f"Status: {r.status_code}, Body: {r.text}")
        assert r.status_code == 403, "Agent should not be able to create tasks in PENDING project"

        print("\n=== Test 5: Admin approves project ===")
        r = client.post(f"/projects/{project_id}/approve", headers={"x-entity-id": str(admin_id)})
        print(f"Status: {r.status_code}, Body: {r.json()}")
        assert r.json()["approval_status"] == "APPROVED"

        print("\n=== Test 6: Agent creates task in APPROVED project (should succeed) ===")
        r = client.post("/tasks", json={
            "title": "Agent Task",
            "project_id": project_id
        }, headers={"x-entity-id": str(agent_id)})
        print(f"Status: {r.status_code}, Body: {r.json()}")
        assert r.status_code == 201
        task = r.json()
        task_id = task["id"]
        assert task["created_by"] == agent_id, "Task should track created_by"

        print("\n=== Test 7: Another agent tries to update task (should fail) ===")
        r = client.post("/entities/register/agent", json={
            "name": f"Other Agent {suffix}",
            "entity_type": "agent"
        }, headers={"x-entity-id": str(admin_id)})
        other_agent_id = r.json()["id"]
        r = client.patch(f"/tasks/{task_id}", json={
            "title": "Hacked Task"
        }, headers={"x-entity-id": str(other_agent_id)})
        print(f"Status: {r.status_code}, Body: {r.text}")
        assert r.status_code == 403, "Other agent should not be able to edit unassigned task"

        print("\n=== Test 8: Task creator updates task (should succeed) ===")
        r = client.patch(f"/tasks/{task_id}", json={
            "title": "Updated Task",
            "version": task["version"]
        }, headers={"x-entity-id": str(agent_id)})
        print(f"Status: {r.status_code}, Body: {r.json()}")
        assert r.status_code == 200

        print("\n=== Test 9: Optimistic locking conflict ===")
        r = client.patch(f"/tasks/{task_id}", json={
            "title": "Conflict Task",
            "version": task["version"]  # old version
        }, headers={"x-entity-id": str(agent_id)})
        print(f"Status: {r.status_code}, Body: {r.text}")
        assert r.status_code == 409, "Should get conflict on stale version"

        print("\n=== Test 10: Reject project ===")
        r2 = client.post("/projects", json={
            "name": "Reject Project",
            "description": "To be rejected"
        }, headers={"x-entity-id": str(admin_id)})
        reject_project_id = r2.json()["id"]
        r = client.post(f"/projects/{reject_project_id}/reject", headers={"x-entity-id": str(admin_id)})
        print(f"Status: {r.status_code}, Body: {r.json()}")
        assert r.json()["approval_status"] == "REJECTED"

        print("\n=== Test 11: No headers on protected endpoint (must 401) ===")
        r = client.post("/projects", json={
            "name": "Hacker Project",
            "description": "Should fail"
        })
        print(f"Status: {r.status_code}, Body: {r.text}")
        assert r.status_code == 401, "Unauthenticated POST must return 401"

        print("\n=== ALL TESTS PASSED ===")

if __name__ == "__main__":
    test_rbac()
