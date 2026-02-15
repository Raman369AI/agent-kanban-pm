#!/usr/bin/env python3
"""
MCP Server for Agent Kanban PM
Allows AI agents to connect via Model Context Protocol for project planning
"""

import asyncio
import json
from typing import Any, Sequence
import sqlite3
from datetime import datetime

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import (
        Tool,
        TextContent,
        ImageContent,
        EmbeddedResource,
        Resource,
        Prompt,
        PromptMessage,
        GetPromptResult,
    )
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False
    print("⚠️  MCP not available. Install with: pip install mcp")


class KanbanMCPServer:
    """MCP Server for Agent Kanban PM system"""
    
    def __init__(self, db_path: str = "kanban.db"):
        self.db_path = db_path
        self.server = Server("agent-kanban-pm")
        self.setup_handlers()
    
    def get_db(self):
        """Get database connection"""
        return sqlite3.connect(self.db_path)
    
    def setup_handlers(self):
        """Setup MCP handlers"""
        
        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            """List available MCP tools for agents"""
            return [
                Tool(
                    name="create_project",
                    description="Create a new project with tasks and stages",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Project name"},
                            "description": {"type": "string", "description": "Project description"},
                            "tasks": {
                                "type": "array",
                                "description": "List of tasks for the project",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "title": {"type": "string"},
                                        "description": {"type": "string"},
                                        "required_skills": {"type": "string"},
                                        "priority": {"type": "integer"}
                                    }
                                }
                            }
                        },
                        "required": ["name"]
                    }
                ),
                Tool(
                    name="get_projects",
                    description="Get all projects with their details",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "status": {
                                "type": "string",
                                "enum": ["pending", "approved", "rejected"],
                                "description": "Filter by approval status"
                            }
                        }
                    }
                ),
                Tool(
                    name="get_project_details",
                    description="Get detailed information about a specific project",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "project_id": {"type": "integer", "description": "Project ID"}
                        },
                        "required": ["project_id"]
                    }
                ),
                Tool(
                    name="create_task",
                    description="Create a new task in a project",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "project_id": {"type": "integer", "description": "Project ID"},
                            "title": {"type": "string", "description": "Task title"},
                            "description": {"type": "string", "description": "Task description"},
                            "required_skills": {"type": "string", "description": "Required skills (comma-separated)"},
                            "priority": {"type": "integer", "description": "Task priority (0-10)"}
                        },
                        "required": ["project_id", "title"]
                    }
                ),
                Tool(
                    name="get_tasks",
                    description="Get tasks with optional filters",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "project_id": {"type": "integer", "description": "Filter by project ID"},
                            "status": {"type": "string", "description": "Filter by task status"}
                        }
                    }
                ),
                Tool(
                    name="approve_project",
                    description="Approve a pending project",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "project_id": {"type": "integer", "description": "Project ID to approve"}
                        },
                        "required": ["project_id"]
                    }
                ),
                Tool(
                    name="plan_project",
                    description="AI-assisted project planning - creates project with intelligent task breakdown",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "goal": {"type": "string", "description": "What you want to accomplish"},
                            "scope": {"type": "string", "description": "Project scope and constraints"},
                            "skills_available": {"type": "string", "description": "Available skills/resources"}
                        },
                        "required": ["goal"]
                    }
                )
            ]
        
        @self.server.call_tool()
        async def call_tool(name: str, arguments: Any) -> Sequence[TextContent]:
            """Handle tool calls from agents"""
            
            try:
                if name == "create_project":
                    return await self._create_project(arguments)
                elif name == "get_projects":
                    return await self._get_projects(arguments)
                elif name == "get_project_details":
                    return await self._get_project_details(arguments)
                elif name == "create_task":
                    return await self._create_task(arguments)
                elif name == "get_tasks":
                    return await self._get_tasks(arguments)
                elif name == "approve_project":
                    return await self._approve_project(arguments)
                elif name == "plan_project":
                    return await self._plan_project(arguments)
                else:
                    return [TextContent(type="text", text=f"Unknown tool: {name}")]
            except Exception as e:
                return [TextContent(type="text", text=f"Error: {str(e)}")]
    
    async def _create_project(self, args: dict) -> Sequence[TextContent]:
        """Create a new project"""
        conn = self.get_db()
        cursor = conn.cursor()
        
        # Create project (entity_id=1 for demo, should come from auth)
        cursor.execute(
            "INSERT INTO projects (name, description, creator_id, approval_status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (args["name"], args.get("description", ""), 1, "pending", datetime.utcnow(), datetime.utcnow())
        )
        project_id = cursor.lastrowid
        
        # Create default stages
        stages = [
            ("Backlog", "Tasks to be done", 1),
            ("To Do", "Ready to start", 2),
            ("In Progress", "Currently being worked on", 3),
            ("Review", "Awaiting review", 4),
            ("Done", "Completed tasks", 5)
        ]
        for name, desc, order in stages:
            cursor.execute(
                "INSERT INTO stages (name, description, 'order', project_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (name, desc, order, project_id, datetime.utcnow())
            )
        
        # Get stage IDs
        cursor.execute("SELECT id FROM stages WHERE project_id = ? ORDER BY 'order'", (project_id,))
        stage_ids = [row[0] for row in cursor.fetchall()]
        todo_stage_id = stage_ids[1] if len(stage_ids) > 1 else stage_ids[0]
        
        # Create tasks if provided
        if "tasks" in args:
            for task in args["tasks"]:
                cursor.execute(
                    "INSERT INTO tasks (title, description, status, project_id, stage_id, required_skills, priority, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        task["title"],
                        task.get("description", ""),
                        "pending",
                        project_id,
                        todo_stage_id,
                        task.get("required_skills", ""),
                        task.get("priority", 0),
                        datetime.utcnow(),
                        datetime.utcnow()
                    )
                )
        
        conn.commit()
        conn.close()
        
        result = {
            "success": True,
            "project_id": project_id,
            "message": f"Project '{args['name']}' created successfully",
            "stages": len(stages),
            "tasks": len(args.get("tasks", []))
        }
        
        return [TextContent(type="text", text=json.dumps(result, indent=2))]
    
    async def _get_projects(self, args: dict) -> Sequence[TextContent]:
        """Get all projects"""
        conn = self.get_db()
        cursor = conn.cursor()
        
        query = "SELECT id, name, description, approval_status, created_at FROM projects"
        params = []
        
        if "status" in args:
            query += " WHERE approval_status = ?"
            params.append(args["status"])
        
        cursor.execute(query, params)
        projects = []
        for row in cursor.fetchall():
            projects.append({
                "id": row[0],
                "name": row[1],
                "description": row[2],
                "approval_status": row[3],
                "created_at": row[4]
            })
        
        conn.close()
        return [TextContent(type="text", text=json.dumps(projects, indent=2))]
    
    async def _get_project_details(self, args: dict) -> Sequence[TextContent]:
        """Get detailed project information"""
        conn = self.get_db()
        cursor = conn.cursor()
        
        # Get project
        cursor.execute("SELECT * FROM projects WHERE id = ?", (args["project_id"],))
        project = cursor.fetchone()
        
        if not project:
            conn.close()
            return [TextContent(type="text", text=json.dumps({"error": "Project not found"}))]
        
        # Get stages
        cursor.execute("SELECT id, name, 'order' FROM stages WHERE project_id = ? ORDER BY 'order'", (args["project_id"],))
        stages = [{"id": row[0], "name": row[1], "order": row[2]} for row in cursor.fetchall()]
        
        # Get tasks
        cursor.execute("SELECT id, title, status, priority, stage_id FROM tasks WHERE project_id = ?", (args["project_id"],))
        tasks = [{"id": row[0], "title": row[1], "status": row[2], "priority": row[3], "stage_id": row[4]} for row in cursor.fetchall()]
        
        conn.close()
        
        result = {
            "project": {
                "id": project[0],
                "name": project[1],
                "description": project[2],
                "approval_status": project[4],
            },
            "stages": stages,
            "tasks": tasks
        }
        
        return [TextContent(type="text", text=json.dumps(result, indent=2))]
    
    async def _create_task(self, args: dict) -> Sequence[TextContent]:
        """Create a new task"""
        conn = self.get_db()
        cursor = conn.cursor()
        
        # Get default stage (To Do)
        cursor.execute("SELECT id FROM stages WHERE project_id = ? ORDER BY 'order' LIMIT 1 OFFSET 1", (args["project_id"],))
        stage = cursor.fetchone()
        stage_id = stage[0] if stage else None
        
        cursor.execute(
            "INSERT INTO tasks (title, description, status, project_id, stage_id, required_skills, priority, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                args["title"],
                args.get("description", ""),
                "pending",
                args["project_id"],
                stage_id,
                args.get("required_skills", ""),
                args.get("priority", 0),
                datetime.utcnow(),
                datetime.utcnow()
            )
        )
        task_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        result = {"success": True, "task_id": task_id, "title": args["title"]}
        return [TextContent(type="text", text=json.dumps(result, indent=2))]
    
    async def _get_tasks(self, args: dict) -> Sequence[TextContent]:
        """Get tasks with filters"""
        conn = self.get_db()
        cursor = conn.cursor()
        
        query = "SELECT id, title, description, status, project_id, priority FROM tasks WHERE 1=1"
        params = []
        
        if "project_id" in args:
            query += " AND project_id = ?"
            params.append(args["project_id"])
        
        if "status" in args:
            query += " AND status = ?"
            params.append(args["status"])
        
        cursor.execute(query, params)
        tasks = []
        for row in cursor.fetchall():
            tasks.append({
                "id": row[0],
                "title": row[1],
                "description": row[2],
                "status": row[3],
                "project_id": row[4],
                "priority": row[5]
            })
        
        conn.close()
        return [TextContent(type="text", text=json.dumps(tasks, indent=2))]
    
    async def _approve_project(self, args: dict) -> Sequence[TextContent]:
        """Approve a project"""
        conn = self.get_db()
        cursor = conn.cursor()
        
        cursor.execute(
            "UPDATE projects SET approval_status = 'approved', updated_at = ? WHERE id = ?",
            (datetime.utcnow(), args["project_id"])
        )
        conn.commit()
        conn.close()
        
        result = {"success": True, "project_id": args["project_id"], "status": "approved"}
        return [TextContent(type="text", text=json.dumps(result, indent=2))]
    
    async def _plan_project(self, args: dict) -> Sequence[TextContent]:
        """AI-assisted project planning"""
        # This is a template - the agent will use this to create a structured plan
        plan_template = {
            "project": {
                "name": f"Project: {args['goal']}",
                "description": args.get('scope', 'AI-generated project plan'),
                "goal": args['goal']
            },
            "suggested_tasks": [
                {
                    "title": "Define Requirements",
                    "description": "Clearly define all project requirements and constraints",
                    "priority": 10,
                    "required_skills": "planning,analysis"
                },
                {
                    "title": "Design Architecture",
                    "description": "Design the system architecture and data models",
                    "priority": 9,
                    "required_skills": "design,architecture"
                },
                {
                    "title": "Implementation",
                    "description": "Implement the core functionality",
                    "priority": 8,
                    "required_skills": args.get('skills_available', 'development')
                },
                {
                    "title": "Testing",
                    "description": "Write and run comprehensive tests",
                    "priority": 7,
                    "required_skills": "testing,qa"
                },
                {
                    "title": "Documentation",
                    "description": "Create user and technical documentation",
                    "priority": 6,
                    "required_skills": "documentation,writing"
                }
            ],
            "instructions": "Use 'create_project' tool with the above structure to create the planned project"
        }
        
        return [TextContent(type="text", text=json.dumps(plan_template, indent=2))]
    
    async def run(self):
        """Run the MCP server"""
        async with stdio_server() as (read_stream, write_stream):
            await self.server.run(read_stream, write_stream, self.server.create_initialization_options())


async def main():
    """Main entry point"""
    if not MCP_AVAILABLE:
        print("Error: MCP library not available")
        print("Install with: pip install mcp")
        return 1
    
    server = KanbanMCPServer()
    await server.run()


if __name__ == "__main__":
    asyncio.run(main())
