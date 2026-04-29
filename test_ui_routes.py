import subprocess
import time
import urllib.request
import urllib.error
import json
import os
import sys

# Subprocess-driven test (server runs in a child process); cleanup via DB wipe below.
# tests_helper is a no-op here but kept for consistency with other test files.
sys.path.append(os.getcwd())
import tests_helper  # noqa: F401

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
    sys.exit(1)

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

print("=" * 60)
print("UI ROUTE CHECKS")
print("=" * 60)

routes = [
    ("GET", "/", "Root / Dashboard"),
    ("GET", "/ui/projects", "Projects list"),
    ("GET", "/ui/projects/1/board", "Kanban board (no project yet)"),
    ("GET", "/ui/users", "Users page"),
    ("GET", "/ui/register", "Register page"),
    ("GET", "/static/css/style.css", "Main CSS"),
    ("GET", "/static/css/kanban_premium.css", "Kanban CSS"),
    ("GET", "/static/js/main.js", "Main JS"),
    ("GET", "/docs", "API Docs"),
]

all_ok = True
for method, path, desc in routes:
    status, body = fetch(f"{base}{path}", method=method)
    ok = status == 200
    if path == "/ui/projects/1/board" and status == 404:
        ok = True  # Expected when no project exists
    symbol = "✅" if ok else "❌"
    print(f"{symbol} {desc:30s} → HTTP {status}")
    if not ok:
        all_ok = False

print("\n" + "=" * 60)
print("CREATING TEST DATA")
print("=" * 60)

# Register human
status, body = fetch(f"{base}/entities/register/human", "POST", {"name": "Test User", "entity_type": "human"})
human = json.loads(body)
human_id = human["id"]
print(f"Human registered: id={human_id}, role={human['role']}")

# Create project
status, body = fetch(f"{base}/projects", "POST", {"name": "UI Test", "description": "Testing UI"}, {"x-entity-id": str(human_id)})
project = json.loads(body)
project_id = project["id"]
print(f"Project created: id={project_id}, status={project['approval_status']}")

# Approve project
status, body = fetch(f"{base}/projects/{project_id}/approve", "POST", {}, {"x-entity-id": str(human_id)})
approved = json.loads(body)
print(f"Project approved: {approved['approval_status']}")

# Create task
status, body = fetch(f"{base}/tasks", "POST", {"title": "UI Test Task", "project_id": project_id}, {"x-entity-id": str(human_id)})
task = json.loads(body)
print(f"Task created: id={task['id']}")

print("\n" + "=" * 60)
print("BOARD CHECK WITH PROJECT")
print("=" * 60)

status, body = fetch(f"{base}/ui/projects/{project_id}/board")
has_panel = "agent-activity-panel" in body
has_sidebar = "agent-activity-sidebar" in body
print(f"Board status: {status}")
print(f"Has activity panel: {'✅ YES' if has_panel else '❌ NO'}")
print(f"Has sidebar: {'✅ YES' if has_sidebar else '❌ NO'}")

# Check for any template errors
template_errors = ["TemplateNotFound", "Jinja2", "undefined", "Traceback"]
for err in template_errors:
    if err.lower() in body.lower():
        print(f"❌ Template error found: {err}")
        all_ok = False

print("\n" + "=" * 60)
if all_ok:
    print("✅ ALL UI ROUTES ARE ACCESSIBLE")
else:
    print("❌ SOME UI ROUTES FAILED")
print("=" * 60)

print("\nAccess URLs:")
print(f"  Dashboard:   http://localhost:8000/")
print(f"  Projects:    http://localhost:8000/ui/projects")
print(f"  Board:       http://localhost:8000/ui/projects/{project_id}/board")
print(f"  API Docs:    http://localhost:8000/docs")

# Cleanup
proc.terminate()
try:
    proc.wait(timeout=5)
except subprocess.TimeoutExpired:
    proc.kill()
print(f"\nServer stopped (PID {proc.pid})")
