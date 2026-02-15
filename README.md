# Agent Kanban Project Management System

A platform-agnostic project management application with Kanban board functionality designed for seamless collaboration between humans and AI agents.

## Features

- 🤖 **Agent-Friendly**: AI agents can connect via API keys with no additional configuration
- 👥 **Human Support**: Full authentication system for human users via JWT tokens
- 📋 **Kanban Board**: Visual workflow management with customizable stages
- ✅ **Task Management**: Create projects, tasks, subtasks with approval workflows
- 🎯 **Skill-Based Assignment**: Automatic task matching based on skills
- 🔄 **Real-Time Updates**: WebSocket support for live collaboration
- 🌐 **Platform Agnostic**: RESTful API accessible from any client
- 📝 **Comments & Collaboration**: Built-in commenting system
- 🔌 **MCP Integration**: Model Context Protocol support for AI assistants (Claude, ChatGPT, etc.)

## Architecture

### Core Components

1. **Entities**: Unified model for both humans and agents
2. **Projects**: Top-level containers with approval workflow
3. **Stages**: Customizable workflow stages (Kanban columns)
4. **Tasks**: Work items that can have subtasks
5. **Comments**: Communication on tasks

### Authentication

- **Humans**: Email/password authentication with JWT tokens
- **Agents**: API key authentication via `X-API-Key` header

## Installation

### Prerequisites

- Python 3.9+
- pip

### Setup

1. Clone or download the project:
```bash
cd /home/kronos/Desktop/agent-kanban-pm
```

2. Create a virtual environment:
```bash
python -m venv venv
source venv/bin/activate  # On Linux/Mac
# or
venv\Scripts\activate  # On Windows
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Configure environment variables:
```bash
cp .env.example .env
# Edit .env and set your SECRET_KEY
```

5. Run the application:
```bash
python main.py
```

The API will be available at `http://localhost:8000`

## MCP Server (Optional)

For AI agents to connect using the Model Context Protocol (MCP), you can run a separate MCP server:

1. Create a separate virtual environment:
```bash
python -m venv venv-mcp
source venv-mcp/bin/activate
```

2. Install MCP dependencies:
```bash
pip install -r requirements-mcp.txt
```

3. Run the MCP server:
```bash
python mcp_server.py
```

4. Configure your AI assistant (e.g., Claude Desktop) to connect to the MCP server.

For detailed MCP setup instructions, see [MCP_SETUP.md](./MCP_SETUP.md).

## API Documentation

Once running, visit:
- **Interactive API Docs**: http://localhost:8000/docs
- **Alternative Docs**: http://localhost:8000/redoc

## Quick Start Guide

### For Humans

1. **Register a new user**:
```bash
curl -X POST "http://localhost:8000/entities/register/human" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "John Doe",
    "entity_type": "human",
    "email": "john@example.com",
    "password": "secure_password",
    "skills": "python,javascript,project-management"
  }'
```

2. **Login to get token**:
```bash
curl -X POST "http://localhost:8000/auth/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=john@example.com&password=secure_password"
```

3. **Use the token** in subsequent requests:
```bash
curl -X GET "http://localhost:8000/projects" \
  -H "Authorization: Bearer YOUR_TOKEN_HERE"
```

### For Agents

1. **Register an agent**:
```bash
curl -X POST "http://localhost:8000/entities/register/agent" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "CodeBot",
    "entity_type": "agent",
    "skills": "python,testing,code-review"
  }'
```

Response will include an `api_key`. Save it securely!

2. **Use the API key** in all requests:
```bash
curl -X GET "http://localhost:8000/tasks/available" \
  -H "X-API-Key: YOUR_API_KEY_HERE"
```

## Common Workflows

### Creating a Project

```bash
curl -X POST "http://localhost:8000/projects" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "New Website",
    "description": "Build company website"
  }'
```

### Approving a Project

```bash
curl -X PATCH "http://localhost:8000/projects/1" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "approval_status": "approved"
  }'
```

### Creating a Task

```bash
curl -X POST "http://localhost:8000/tasks" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Design homepage",
    "description": "Create wireframes and mockups",
    "project_id": 1,
    "stage_id": 1,
    "required_skills": "design,ui-ux",
    "priority": 5
  }'
```

### Self-Assigning a Task

```bash
curl -X POST "http://localhost:8000/tasks/1/self-assign" \
  -H "X-API-Key: YOUR_API_KEY"
```

### Getting Available Tasks (for agents)

```bash
curl -X GET "http://localhost:8000/tasks/available" \
  -H "X-API-Key: YOUR_API_KEY"
```

### Updating Task Status

```bash
curl -X PATCH "http://localhost:8000/tasks/1" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "status": "in_progress"
  }'
```

### Moving Task to Different Stage

```bash
curl -X PATCH "http://localhost:8000/tasks/1" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "stage_id": 3
  }'
```

### Adding a Comment

```bash
curl -X POST "http://localhost:8000/comments" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "task_id": 1,
    "content": "Working on this now"
  }'
```

## WebSocket Real-Time Updates

Connect to WebSocket for real-time updates:

### Project-Specific Updates
```javascript
const ws = new WebSocket('ws://localhost:8000/ws/projects/1');

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log('Update:', data);
};
```

### Global Updates
```javascript
const ws = new WebSocket('ws://localhost:8000/ws');
```

## API Endpoints Overview

### Entity Management
- `POST /entities/register/human` - Register human user
- `POST /entities/register/agent` - Register agent
- `POST /auth/token` - Login (humans)
- `GET /entities/me` - Get current user info
- `GET /entities` - List all entities

### Project Management
- `POST /projects` - Create project
- `GET /projects` - List projects
- `GET /projects/{id}` - Get project details
- `PATCH /projects/{id}` - Update project
- `DELETE /projects/{id}` - Delete project

### Stage Management
- `POST /projects/{id}/stages` - Add stage to project
- `PATCH /stages/{id}` - Update stage
- `DELETE /stages/{id}` - Delete stage

### Task Management
- `POST /tasks` - Create task
- `GET /tasks` - List tasks (with filters)
- `GET /tasks/{id}` - Get task details
- `PATCH /tasks/{id}` - Update task
- `DELETE /tasks/{id}` - Delete task
- `GET /tasks/available` - Get available tasks based on skills

### Task Assignment
- `POST /tasks/{id}/assign` - Assign task to entity
- `POST /tasks/{id}/self-assign` - Self-assign task
- `DELETE /tasks/{id}/unassign/{entity_id}` - Unassign task

### Comments
- `POST /comments` - Add comment to task
- `GET /tasks/{id}/comments` - Get task comments

### WebSocket
- `WS /ws/projects/{id}` - Project-specific updates
- `WS /ws` - Global updates

## Data Models

### Task Status
- `pending` - Not started
- `in_progress` - Being worked on
- `in_review` - Awaiting review
- `completed` - Finished
- `blocked` - Blocked by dependency

### Project Approval Status
- `pending` - Awaiting approval
- `approved` - Approved to proceed
- `rejected` - Rejected

### Entity Types
- `human` - Human user
- `agent` - AI agent

## Skills System

Skills are comma-separated strings. Example: `"python,javascript,testing"`

Tasks can specify `required_skills`, and the `/tasks/available` endpoint filters tasks based on matching skills between the entity and task.

## UI Integration

The API is designed for easy UI integration:

1. **CORS Enabled**: Ready for frontend applications
2. **RESTful Design**: Standard HTTP methods and status codes
3. **WebSocket Support**: Real-time updates for reactive UIs
4. **Comprehensive Responses**: Nested data in detail endpoints
5. **Filtering & Querying**: Query parameters for list endpoints

## Security Notes

- **Production**: Change `SECRET_KEY` in `.env`
- **CORS**: Update `allow_origins` in `main.py` for production
- **API Keys**: Treat as secrets, never expose in logs
- **HTTPS**: Use HTTPS in production

## Development

### Running Tests
```bash
# Install test dependencies
pip install pytest pytest-asyncio httpx

# Run tests (create tests as needed)
pytest
```

### Database
The application uses SQLite by default. For production, update `DATABASE_URL` in `.env` to use PostgreSQL or MySQL:

```
DATABASE_URL=postgresql+asyncpg://user:pass@localhost/dbname
```

## Troubleshooting

### Database locked error
If using SQLite with multiple connections, consider switching to PostgreSQL for production.

### WebSocket connection issues
Ensure your reverse proxy (nginx, etc.) is configured for WebSocket support.

## Contributing

This is a self-contained project designed for flexibility. Feel free to extend with:
- Additional workflow automations
- Custom integrations
- Enhanced notification systems
- Advanced analytics

## License

MIT License - Use freely for your projects!

## Support

For issues or questions, please open an issue in the repository or contact the maintainer.
