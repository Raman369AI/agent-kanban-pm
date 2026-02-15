import requests
import json

BASE_URL = "http://localhost:8000"

def test_assignment():
    # 1. Get entities
    print("Getting entities...")
    response = requests.get(f"{BASE_URL}/ui/api/entities")
    if response.status_code != 200:
        print(f"Failed to get entities: {response.status_code}")
        return
    entities = response.json()
    if not entities:
        print("No entities found. Creating one...")
        # Create entity
        requests.post(f"{BASE_URL}/ui/register", data={
            "name": "Test Agent",
            "email": "agent@test.com",
            "password": "password",
            "skills": "python"
        })
        entities = requests.get(f"{BASE_URL}/ui/api/entities").json()
    
    agent = entities[0]
    print(f"Using entity: {agent['id']} ({agent['name']})")
    
    # 2. Get tasks (assume project 2 exists from user report)
    print("Getting tasks...")
    # We don't have a direct API to list tasks for project, use /ui/projects/2/board which returns HTML
    # But /routers/tasks.py has GET /tasks
    response = requests.get(f"{BASE_URL}/tasks?project_id=2")
    tasks = response.json()
    if not tasks:
        print("No tasks found for project 2. Trying generic /tasks...")
        response = requests.get(f"{BASE_URL}/tasks")
        tasks = response.json()
        
    if not tasks:
        print("No tasks found at all.")
        return

    task = tasks[0]
    print(f"Using task: {task['id']} ({task['title']})")
    
    # 3. Assign
    print(f"Assigning entity {agent['id']} to task {task['id']}...")
    url = f"{BASE_URL}/ui/tasks/{task['id']}/assign"
    data = {"entity_id": agent['id'], "action": "assign"}
    response = requests.post(url, json=data)
    
    print(f"Response: {response.status_code} {response.text}")
    
    # 4. Verify
    print("Verifying assignment...")
    response = requests.get(f"{BASE_URL}/tasks/{task['id']}")
    task_detail = response.json()
    assignees = task_detail.get("assignees", [])
    print(f"Assignees: {assignees}")
    
    assigned = any(a['id'] == agent['id'] for a in assignees)
    if assigned:
        print("SUCCESS: Entity assigned.")
    else:
        print("FAILURE: Entity not assigned.")

if __name__ == "__main__":
    test_assignment()
