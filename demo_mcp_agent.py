#!/usr/bin/env python3
"""
Demo script showing how an AI agent would interact with the Kanban system via MCP

This is a simulation of what an AI agent would do through MCP tools.
In reality, the AI assistant (like Claude) would call these tools directly.
"""

import json
import sqlite3
from datetime import datetime


def demo_agent_workflow():
    """Simulate an agent using MCP tools to create and manage a project"""
    
    print("=" * 70)
    print("DEMO: AI Agent Using MCP to Create and Manage a Project")
    print("=" * 70)
    print()
    
    # Connect to database
    conn = sqlite3.connect("kanban.db")
    cursor = conn.cursor()
    
    # Step 1: Agent plans a project
    print("Step 1: Agent uses 'plan_project' tool")
    print("-" * 70)
    plan_input = {
        "goal": "Build a REST API for user management",
        "scope": "Authentication, CRUD operations, user profiles, PostgreSQL database",
        "skills_available": "python,fastapi,postgresql,security"
    }
    print(f"Input: {json.dumps(plan_input, indent=2)}")
    print()
    
    # Simulate plan_project response
    plan_template = {
        "project": {
            "name": "REST API for User Management",
            "description": "Build a secure REST API with user authentication and CRUD operations",
            "goal": plan_input["goal"]
        },
        "suggested_tasks": [
            {
                "title": "Setup FastAPI project structure",
                "description": "Initialize FastAPI app with proper project layout",
                "priority": 10,
                "required_skills": "python,fastapi"
            },
            {
                "title": "Design database schema",
                "description": "Create user table, auth tables, and relationships",
                "priority": 9,
                "required_skills": "postgresql,database-design"
            },
            {
                "title": "Implement JWT authentication",
                "description": "Add login, token generation, and validation",
                "priority": 8,
                "required_skills": "python,fastapi,security"
            },
            {
                "title": "Build CRUD endpoints",
                "description": "Implement user creation, retrieval, update, delete",
                "priority": 7,
                "required_skills": "python,fastapi,postgresql"
            },
            {
                "title": "Add input validation",
                "description": "Validate user input with Pydantic models",
                "priority": 6,
                "required_skills": "python,fastapi"
            },
            {
                "title": "Write unit tests",
                "description": "Test all endpoints with pytest",
                "priority": 5,
                "required_skills": "python,testing"
            }
        ]
    }
    print(f"Agent received plan:\n{json.dumps(plan_template, indent=2)}")
    print()
    
    # Step 2: Agent creates the project
    print("\nStep 2: Agent uses 'create_project' tool")
    print("-" * 70)
    create_project_input = {
        "name": plan_template["project"]["name"],
        "description": plan_template["project"]["description"],
        "tasks": plan_template["suggested_tasks"]
    }
    print(f"Creating project with {len(create_project_input['tasks'])} tasks...")
    
    # Actually create the project
    cursor.execute(
        "INSERT INTO projects (name, description, creator_id, approval_status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            create_project_input["name"],
            create_project_input["description"],
            1,  # Demo entity
            "pending",
            datetime.utcnow(),
            datetime.utcnow()
        )
    )
    project_id = cursor.lastrowid
    
    # Create stages
    stages = [
        ("Backlog", "Tasks to be done", 1),
        ("To Do", "Ready to start", 2),
        ("In Progress", "Currently being worked on", 3),
        ("Review", "Awaiting review", 4),
        ("Done", "Completed tasks", 5)
    ]
    stage_ids = []
    for name, desc, order in stages:
        cursor.execute(
            "INSERT INTO stages (name, description, 'order', project_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, desc, order, project_id, datetime.utcnow())
        )
        stage_ids.append(cursor.lastrowid)
    
    # Create tasks
    todo_stage_id = stage_ids[1]  # To Do stage
    for task in create_project_input["tasks"]:
        cursor.execute(
            "INSERT INTO tasks (title, description, status, project_id, stage_id, required_skills, priority, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task["title"],
                task["description"],
                "pending",
                project_id,
                todo_stage_id,
                task["required_skills"],
                task["priority"],
                datetime.utcnow(),
                datetime.utcnow()
            )
        )
    
    conn.commit()
    print(f"✓ Project created! ID: {project_id}")
    print(f"✓ Created {len(stages)} stages")
    print(f"✓ Created {len(create_project_input['tasks'])} tasks")
    print()
    
    # Step 3: Agent queries project details
    print("\nStep 3: Agent uses 'get_project_details' tool")
    print("-" * 70)
    cursor.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    project = cursor.fetchone()
    
    cursor.execute("SELECT id, name FROM stages WHERE project_id = ? ORDER BY 'order'", (project_id,))
    stages_result = cursor.fetchall()
    
    cursor.execute("SELECT id, title, priority, required_skills FROM tasks WHERE project_id = ?", (project_id,))
    tasks_result = cursor.fetchall()
    
    project_details = {
        "project": {
            "id": project[0],
            "name": project[1],
            "description": project[2],
            "approval_status": project[4]
        },
        "stages": [{"id": s[0], "name": s[1]} for s in stages_result],
        "tasks": [
            {
                "id": t[0],
                "title": t[1],
                "priority": t[2],
                "required_skills": t[3]
            } for t in tasks_result
        ]
    }
    
    print(f"Agent retrieved project details:\n{json.dumps(project_details, indent=2)}")
    print()
    
    # Step 4: Human approves the project (simulated)
    print("\nStep 4: Human reviews and approves project via web UI")
    print("-" * 70)
    print("(In reality, human would use the web UI at http://localhost:8000)")
    print("Simulating approval...")
    
    cursor.execute(
        "UPDATE projects SET approval_status = 'approved', updated_at = ? WHERE id = ?",
        (datetime.utcnow(), project_id)
    )
    conn.commit()
    print("✓ Project approved!")
    print()
    
    # Step 5: Agent checks for available tasks
    print("\nStep 5: Agent queries available tasks matching its skills")
    print("-" * 70)
    agent_skills = ["python", "fastapi", "postgresql"]
    print(f"Agent skills: {', '.join(agent_skills)}")
    
    cursor.execute("""
        SELECT id, title, required_skills, priority 
        FROM tasks 
        WHERE project_id = ? AND status = 'pending'
        ORDER BY priority DESC
    """, (project_id,))
    
    available_tasks = []
    for row in cursor.fetchall():
        task_skills = row[2].split(",")
        # Check if agent has any matching skills
        if any(skill.strip() in agent_skills for skill in task_skills):
            available_tasks.append({
                "id": row[0],
                "title": row[1],
                "required_skills": row[2],
                "priority": row[3]
            })
    
    print(f"\nFound {len(available_tasks)} matching tasks:")
    for task in available_tasks[:3]:  # Show top 3
        print(f"  - {task['title']} (priority: {task['priority']}, skills: {task['required_skills']})")
    print()
    
    # Summary
    print("\n" + "=" * 70)
    print("DEMO COMPLETE")
    print("=" * 70)
    print()
    print("Summary:")
    print(f"  • Agent created a project with {len(create_project_input['tasks'])} tasks")
    print(f"  • Project ID: {project_id}")
    print(f"  • Project status: approved")
    print(f"  • Available tasks for agent: {len(available_tasks)}")
    print()
    print("Next steps:")
    print("  1. Agent could self-assign high-priority tasks")
    print("  2. Multiple agents could collaborate on different tasks")
    print("  3. Agents report progress via MCP tools")
    print("  4. Humans monitor via web UI at http://localhost:8000")
    print()
    
    conn.close()


if __name__ == "__main__":
    try:
        demo_agent_workflow()
    except Exception as e:
        print(f"Error: {e}")
        print("\nNote: Make sure the database exists. Run 'python main.py' first to initialize.")
