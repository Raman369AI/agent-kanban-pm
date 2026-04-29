#!/usr/bin/env python3
"""
UI Task Creation Test

Simulates the browser JavaScript flow to verify task creation works
correctly with x-entity-id headers.
"""

import subprocess
import time
import os
import sys
import json
import urllib.request
import urllib.error

# Subprocess-driven test (server runs in a child process); the test wipes
# kanban.db itself. tests_helper is harmless here but kept for consistency.
sys.path.append(os.getcwd())
import tests_helper  # noqa: F401

def run_test():
    # Clean up
    os.system("pkill -f 'python3 main.py' 2>/dev/null")
    time.sleep(1)
    if os.path.exists("kanban.db"):
        os.remove("kanban.db")
    if os.path.exists("server.log"):
        os.remove("server.log")

    # Start server
    proc = subprocess.Popen(
        [sys.executable, "main.py"],
        stdout=open("server.log", "w"),
        stderr=subprocess.STDOUT,
        start_new_session=True
    )
    print(f"Server PID: {proc.pid}")

    # Wait for startup
    for i in range(15):
        time.sleep(0.5)
        try:
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
        return False

    def fetch(url, method="GET", data=None, headers=None):
        h = headers or {}
        if data:
            data = json.dumps(data).encode()
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()
        except Exception as e:
            return -1, str(e)

    base = "http://localhost:8000"
    all_ok = True

    print("=" * 60)
    print("UI TASK CREATION FLOW TEST")
    print("=" * 60)

    # Step 1: Register human
    print("\n1. Register human owner...")
    status, body = fetch(f"{base}/entities/register/human", "POST", {"name": "UI Test User", "entity_type": "human"})
    human = json.loads(body)
    human_id = human["id"]
    print(f"   ✅ Human registered: id={human_id}, role={human['role']}")

    # Step 2: Create and approve project
    print("\n2. Create and approve project...")
    status, body = fetch(f"{base}/projects", "POST", {"name": "UI Test Project", "description": "Test"}, {"x-entity-id": str(human_id)})
    project = json.loads(body)
    project_id = project["id"]
    print(f"   Project created: id={project_id}, status={project['approval_status']}")

    status, body = fetch(f"{base}/projects/{project_id}/approve", "POST", {}, {"x-entity-id": str(human_id)})
    print(f"   ✅ Project approved")

    # Step 3: Verify board page has CURRENT_ENTITY_ID
    print("\n3. Verify board template has CURRENT_ENTITY_ID...")
    req = urllib.request.Request(f"{base}/ui/projects/{project_id}/board")
    with urllib.request.urlopen(req, timeout=5) as resp:
        html = resp.read().decode()
    if "CURRENT_ENTITY_ID = " in html:
        print("   ✅ CURRENT_ENTITY_ID found in board HTML")
    else:
        print("   ❌ CURRENT_ENTITY_ID NOT found in board HTML")
        all_ok = False

    # Step 4: Simulate JS task creation (with x-entity-id header)
    print("\n4. Simulate UI task creation (JS fetch with x-entity-id)...")
    status, body = fetch(
        f"{base}/ui/tasks/create",
        "POST",
        {
            "title": "UI Created Task",
            "project_id": project_id,
            "stage_id": 2,  # To Do stage
            "priority": 5,
            "status": "pending",
            "required_skills": "python"
        },
        {"x-entity-id": str(human_id)}
    )
    if status == 200:
        result = json.loads(body)
        task = result.get("task", result)
        print(f"   ✅ Task created via UI endpoint: id={task['id']}, title={task['title']}")
    else:
        print(f"   ❌ Task creation failed: HTTP {status}, {body}")
        all_ok = False

    # Step 5: Verify task creation WITHOUT x-entity-id fails (401)
    print("\n5. Verify task creation WITHOUT headers fails (401)...")
    status, body = fetch(
        f"{base}/ui/tasks/create",
        "POST",
        {
            "title": "Hacker Task",
            "project_id": project_id,
            "stage_id": 2
        }
    )
    if status == 401:
        print(f"   ✅ Correctly rejected with 401")
    else:
        print(f"   ❌ Expected 401, got {status}: {body}")
        all_ok = False

    # Step 6: Verify projects page has CURRENT_ENTITY_ID
    print("\n6. Verify projects template has CURRENT_ENTITY_ID...")
    req = urllib.request.Request(f"{base}/ui/projects")
    with urllib.request.urlopen(req, timeout=5) as resp:
        html = resp.read().decode()
    if "CURRENT_ENTITY_ID = " in html:
        print("   ✅ CURRENT_ENTITY_ID found in projects HTML")
    else:
        print("   ❌ CURRENT_ENTITY_ID NOT found in projects HTML")
        all_ok = False

    # Step 7: Simulate JS project creation (with x-entity-id header)
    print("\n7. Simulate UI project creation (JS fetch with x-entity-id)...")
    status, body = fetch(
        f"{base}/ui/projects/create",
        "POST",
        {"name": "UI Created Project", "description": "Via fetch"},
        {"x-entity-id": str(human_id)}
    )
    if status == 200:
        new_project = json.loads(body)
        print(f"   ✅ Project created via UI endpoint: id={new_project['id']}, name={new_project['name']}")
    else:
        print(f"   ❌ Project creation failed: HTTP {status}, {body}")
        all_ok = False

    # Step 8: Simulate JS project creation WITHOUT headers fails
    print("\n8. Verify project creation WITHOUT headers fails (401)...")
    status, body = fetch(
        f"{base}/ui/projects/create",
        "POST",
        {"name": "Hacker Project", "description": "Should fail"}
    )
    if status == 401:
        print(f"   ✅ Correctly rejected with 401")
    else:
        print(f"   ❌ Expected 401, got {status}: {body}")
        all_ok = False

    print("\n" + "=" * 60)
    if all_ok:
        print("✅ ALL UI TASK CREATION TESTS PASSED")
    else:
        print("❌ SOME TESTS FAILED")
    print("=" * 60)

    # Cleanup
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    print(f"\nServer stopped (PID {proc.pid})")
    return all_ok

if __name__ == "__main__":
    success = run_test()
    sys.exit(0 if success else 1)
