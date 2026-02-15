# MCP Server Setup for Agent Kanban PM

This document explains how to set up and use the Model Context Protocol (MCP) server for Agent Kanban PM, allowing AI agents to connect and participate in project planning.

## What is MCP?

The Model Context Protocol (MCP) is a standard that allows AI agents to connect to external systems and tools. Our MCP server enables agents to create projects, manage tasks, and participate in collaborative project planning.

## Installation

### Option 1: Using a Separate Virtual Environment (Recommended)

Since MCP requires `anyio>=4.5` and FastAPI requires `anyio<4.0.0`, we recommend running the MCP server in a separate environment:

```bash
# Create a new virtual environment for MCP
python -m venv venv-mcp
source venv-mcp/bin/activate  # On Windows: venv-mcp\Scripts\activate

# Install MCP dependencies
pip install mcp sqlite3
```

### Option 2: Using Docker (Coming Soon)

A Docker container will be available to run the MCP server isolated from the FastAPI app.

## Running the MCP Server

### Standalone Mode

```bash
# Activate the MCP virtual environment
source venv-mcp/bin/activate

# Run the MCP server
python mcp_server.py
```

The server will start and listen for MCP connections via stdio.

### Configuration for AI Assistants

To allow AI assistants like Claude, ChatGPT, or custom agents to connect, you need to configure their MCP settings:

#### For Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "agent-kanban-pm": {
      "command": "python",
      "args": ["/path/to/agent-kanban-pm/mcp_server.py"],
      "env": {
        "VIRTUAL_ENV": "/path/to/venv-mcp"
      }
    }
  }
}
```

#### For Custom Agents

Use the MCP client library to connect:

```python
from mcp.client import Client

async def connect_to_kanban():
    client = Client()
    await client.connect_stdio("python", ["mcp_server.py"])
    return client
```

## Available MCP Tools

The MCP server exposes the following tools to connected agents:

### 1. `create_project`
Create a new project with tasks and stages.

**Parameters:**
- `name` (required): Project name
- `description` (optional): Project description
- `tasks` (optional): Array of task objects

**Example:**
```json
{
  "name": "Build AI Agent",
  "description": "Create an AI agent for task automation",
  "tasks": [
    {
      "title": "Design agent architecture",
      "description": "Plan the AI agent components",
      "required_skills": "ai,design",
      "priority": 10
    }
  ]
}
```

### 2. `get_projects`
Get all projects with optional status filter.

**Parameters:**
- `status` (optional): Filter by "pending", "approved", or "rejected"

### 3. `get_project_details`
Get detailed information about a specific project.

**Parameters:**
- `project_id` (required): Project ID

### 4. `create_task`
Create a new task in a project.

**Parameters:**
- `project_id` (required): Project ID
- `title` (required): Task title
- `description` (optional): Task description
- `required_skills` (optional): Comma-separated skills
- `priority` (optional): Priority (0-10)

### 5. `get_tasks`
Get tasks with optional filters.

**Parameters:**
- `project_id` (optional): Filter by project
- `status` (optional): Filter by status

### 6. `approve_project`
Approve a pending project.

**Parameters:**
- `project_id` (required): Project ID to approve

### 7. `plan_project`
AI-assisted project planning with intelligent task breakdown.

**Parameters:**
- `goal` (required): What you want to accomplish
- `scope` (optional): Project scope and constraints
- `skills_available` (optional): Available skills/resources

**Returns:** A structured project plan template that can be used with `create_project`

## Usage Examples

### Example 1: Agent Creates a Project

```python
# Agent uses MCP to create a project
result = await client.call_tool("plan_project", {
    "goal": "Build a REST API for user management",
    "scope": "Authentication, CRUD operations, database integration",
    "skills_available": "python,fastapi,postgresql"
})

# Agent reviews the plan and creates the project
project = await client.call_tool("create_project", {
    "name": result["project"]["name"],
    "description": result["project"]["description"],
    "tasks": result["suggested_tasks"]
})
```

### Example 2: Query Project Status

```python
# Get all pending projects
projects = await client.call_tool("get_projects", {
    "status": "pending"
})

# Get details of a specific project
details = await client.call_tool("get_project_details", {
    "project_id": 1
})
```

### Example 3: Collaborative Planning

```python
# Multiple agents can participate
agent1 = await connect_to_kanban()
agent2 = await connect_to_kanban()

# Agent 1 creates project
project = await agent1.call_tool("create_project", {...})

# Agent 2 adds tasks
task = await agent2.call_tool("create_task", {
    "project_id": project["project_id"],
    "title": "Additional feature",
    "priority": 8
})
```

## Integration with FastAPI

The MCP server and FastAPI server share the same SQLite database (`kanban.db`), so:

1. Projects created via MCP appear in the FastAPI web UI
2. Human approvals in the web UI are visible to MCP agents
3. Agents can monitor project status changes in real-time

### Running Both Servers

```bash
# Terminal 1: FastAPI server
source venv/bin/activate
uvicorn main:app --reload

# Terminal 2: MCP server
source venv-mcp/bin/activate
python mcp_server.py
```

## Security Considerations

### Current Implementation
- Uses entity_id=1 for demo purposes
- No authentication between agents

### Production Recommendations
1. Implement API key authentication for agent connections
2. Add rate limiting to prevent abuse
3. Use proper entity authentication (link agents to entities)
4. Add audit logging for all MCP operations
5. Consider using WebSocket transport for multi-agent coordination

## Troubleshooting

### "MCP not available" error

```bash
# Make sure you're in the MCP virtual environment
source venv-mcp/bin/activate
pip install mcp
```

### Database locked errors

If you get database locked errors, ensure:
1. Only one process writes to the database at a time
2. Use connection pooling or proper transaction handling
3. Consider switching to PostgreSQL for production

### Connection issues

- Verify the MCP server is running
- Check that the path in your MCP config is correct
- Ensure the virtual environment is properly activated

## Future Enhancements

- [ ] WebSocket transport for real-time collaboration
- [ ] Multi-agent coordination and task assignment
- [ ] Agent-to-agent communication
- [ ] Conflict resolution for concurrent edits
- [ ] Docker container for easy deployment
- [ ] PostgreSQL support for production use
- [ ] Authentication and authorization

## Resources

- [Model Context Protocol Specification](https://modelcontextprotocol.io)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [Agent Kanban PM Documentation](./README.md)
