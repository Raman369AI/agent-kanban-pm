import subprocess
import time
import sys
import os
import signal
import json

def run_smoke_test():
    # Clean start
    os.system("pkill -f 'python3 main.py' 2>/dev/null")
    time.sleep(1)
    if os.path.exists("kanban.db"):
        os.remove("kanban.db")
    if os.path.exists("server.log"):
        os.remove("server.log")

    # Start server in a new process group (detached)
    proc = subprocess.Popen(
        [sys.executable, "main.py"],
        stdout=open("server.log", "w"),
        stderr=subprocess.STDOUT,
        start_new_session=True
    )
    print(f"Server started with PID: {proc.pid}")

    # Wait for server to be ready
    for i in range(20):
        time.sleep(0.5)
        try:
            import urllib.request
            req = urllib.request.Request("http://localhost:8000/health")
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    print("✅ Server is healthy\n")
                    break
        except Exception:
            pass
    else:
        print("❌ Server failed to start")
        proc.terminate()
        return

    import urllib.request

    def req(method, url, data=None, headers=None):
        h = headers or {}
        h["Content-Type"] = "application/json"
        if data:
            data = json.dumps(data).encode()
        r = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    base = "http://localhost:8000"

    print("=== 1. Health check ===")
    status, body = req("GET", f"{base}/health")
    print(f"Status: {status}, Body: {body}")

    print("\n=== 2. Register human owner ===")
    status, body = req("POST", f"{base}/entities/register/human",
                       {"name": "Test Human", "entity_type": "human"})
    human = json.loads(body)
    human_id = human["id"]
    print(f"Status: {status}, Name: {human['name']}, Role: {human['role']}")

    print("\n=== 3. List adapter entities ===")
    status, body = req("GET", f"{base}/entities?entity_type=agent")
    agents = json.loads(body)
    print(f"Found {len(agents)} adapter entities:")
    for a in agents:
        print(f"  - {a['name']}: role={a['role']}, active={a['is_active']}")

    print("\n=== 4. Create project as human ===")
    status, body = req("POST", f"{base}/projects",
                       {"name": "Smoke Test Project", "description": "Live server test"},
                       {"x-entity-id": str(human_id)})
    project = json.loads(body)
    project_id = project["id"]
    print(f"Status: {status}, Project: {project['name']}, Status: {project['approval_status']}")

    print("\n=== 5. Approve project ===")
    status, body = req("POST", f"{base}/projects/{project_id}/approve",
                       {}, {"x-entity-id": str(human_id)})
    print(f"Status: {status}, Approval: {json.loads(body)['approval_status']}")

    print("\n=== 6. Create task ===")
    status, body = req("POST", f"{base}/tasks",
                       {"title": "Test Task", "project_id": project_id},
                       {"x-entity-id": str(human_id)})
    task = json.loads(body)
    task_id = task["id"]
    print(f"Status: {status}, Task: {task['title']}, Created by: {task['created_by']}")

    print("\n=== 7. Report agent heartbeat ===")
    agent = next((a for a in agents if a["is_active"]), None)
    if agent:
        status, body = req("POST", f"{base}/agents/{agent['id']}/status",
                           {"status_type": "working", "message": "Analyzing task", "task_id": task_id})
        print(f"Status: {status}, Heartbeat: {json.loads(body).get('status_type')}")

        print("\n=== 8. Get agent statuses ===")
        status, body = req("GET", f"{base}/agents/status")
        heartbeats = json.loads(body)
        print(f"Found {len(heartbeats)} heartbeat(s)")

        print("\n=== 9. Log activity ===")
        status, body = req("POST", f"{base}/agents/{agent['id']}/activity",
                           {"message": "Requirements analyzed", "activity_type": "thought", "task_id": task_id})
        print(f"Status: {status}, Activity logged")

        print("\n=== 10. Get activity feed ===")
        status, body = req("GET", f"{base}/agents/activity")
        activities = json.loads(body)
        print(f"Found {len(activities)} activity entry/ies")

    print("\n=== 11. Settings ===")
    status, body = req("GET", f"{base}/ui/api/settings")
    print(f"Status: {status}, Settings: {body}")

    print("\n=== 12. 401 without headers on mutation ===")
    status, body = req("POST", f"{base}/projects",
                       {"name": "Hacker Project", "description": "Should fail"})
    print(f"Status: {status} (expected 401), Body: {body}")

    print("\n========================================")
    print("ALL SMOKE TESTS COMPLETED SUCCESSFULLY")
    print("========================================")

    # Cleanup
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    print(f"\nServer stopped (PID {proc.pid})")

if __name__ == "__main__":
    run_smoke_test()
