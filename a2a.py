import asyncio
import json
import logging
from typing import Dict, List, Any, Optional
from datetime import datetime
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/a2a", tags=["Agent-to-Agent"])

class MessageCreate(BaseModel):
    sender_id: int
    receiver_id: int
    message_type: str
    data: Dict[str, Any]


# Agent Card for /.well-known/agent.json discovery
AGENT_CARD = {
    "name": "Agent Kanban PM",
    "description": "A platform-agnostic project management system for humans and AI agents. "
                   "Supports task creation, assignment, lifecycle tracking, and real-time event notifications.",
    "url": "http://localhost:8000",
    "version": "1.0.0",
    "capabilities": {
        "streaming": False,
        "pushNotifications": True,
    },
    "skills": [
        {
            "id": "task-management",
            "name": "Task Management",
            "description": "Create, update, move, assign, and delete tasks across projects"
        },
        {
            "id": "project-management",
            "name": "Project Management",
            "description": "Create and manage projects with configurable stages and approval workflows"
        },
        {
            "id": "event-notifications",
            "name": "Event Notifications",
            "description": "Subscribe to real-time task and project lifecycle events via WebSocket, Webhook, or MCP"
        }
    ],
    "protocols": ["websocket", "webhook", "mcp", "a2a", "rest"],
    "authentication": {
        "schemes": ["apiKey"],
        "apiKey": {
            "headerName": "X-API-Key",
            "description": "Register an agent via POST /entities/register/agent to obtain an API key"
        }
    }
}


@router.get("/.well-known/agent.json")
async def agent_card():
    """A2A Agent Card for discovery"""
    return AGENT_CARD


class A2AManager:
    """Manages Agent-to-Agent communication and discovery"""

    def __init__(self):
        # agent_id -> {status, last_seen, capabilities, endpoint}
        self.directory: Dict[int, Dict[str, Any]] = {}
        self.message_queues: Dict[int, asyncio.Queue] = {}

    def register_agent(self, agent_id: int, capabilities: List[str], endpoint: Optional[str] = None):
        """Register an agent in the discovery directory"""
        self.directory[agent_id] = {
            "status": "online",
            "last_seen": datetime.utcnow().isoformat(),
            "capabilities": capabilities,
            "endpoint": endpoint
        }
        if agent_id not in self.message_queues:
            self.message_queues[agent_id] = asyncio.Queue()
        logger.info(f"Agent {agent_id} registered for A2A")

    def unregister_agent(self, agent_id: int):
        """Unregister an agent"""
        if agent_id in self.directory:
            self.directory[agent_id]["status"] = "offline"

    def discover_agents(self, capability: Optional[str] = None) -> List[Dict[str, Any]]:
        """Find agents by capability"""
        results = []
        for agent_id, info in self.directory.items():
            if info["status"] == "online":
                if capability is None or capability in info["capabilities"]:
                    results.append({"agent_id": agent_id, **info})
        return results

    async def send_message(self, sender_id: int, receiver_id: int, message_type: str, data: Dict[str, Any]):
        """Send a direct message to another agent"""
        message = {
            "type": "a2a_message",
            "sender_id": sender_id,
            "receiver_id": receiver_id,
            "message_type": message_type,
            "timestamp": datetime.utcnow().isoformat(),
            "data": data
        }

        if receiver_id in self.message_queues:
            await self.message_queues[receiver_id].put(message)
            return True
        return False

    async def get_messages(self, agent_id: int) -> List[Dict[str, Any]]:
        """Get pending messages for an agent"""
        if agent_id not in self.message_queues:
            return []

        messages = []
        while not self.message_queues[agent_id].empty():
            messages.append(await self.message_queues[agent_id].get())
        return messages


# Global instance
a2a_manager = A2AManager()

@router.get("/agents")
async def list_agents(capability: Optional[str] = None):
    """List available agents with optional capability filter"""
    return a2a_manager.discover_agents(capability)

@router.post("/messages")
async def send_message(msg: MessageCreate):
    """Send a direct message to another agent"""
    success = await a2a_manager.send_message(
        msg.sender_id, 
        msg.receiver_id, 
        msg.message_type, 
        msg.data
    )
    if not success:
        if msg.receiver_id not in a2a_manager.message_queues:
             raise HTTPException(status_code=404, detail="Receiver agent not found or offline")
    return {"status": "sent"}

@router.get("/messages/{agent_id}")
async def get_messages(agent_id: int):
    """Poll for messages for a specific agent"""
    messages = await a2a_manager.get_messages(agent_id)
    return messages

@router.post("/agents/register")
async def register_a2a_agent(request: Request):
    """Register an agent for A2A communication"""
    body = await request.json()
    agent_id = body.get("agent_id")
    capabilities = body.get("capabilities", [])
    endpoint = body.get("endpoint")

    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id is required")

    a2a_manager.register_agent(agent_id, capabilities, endpoint)
    return {"status": "registered", "agent_id": agent_id}

@router.post("/agents/{agent_id}/unregister")
async def unregister_a2a_agent(agent_id: int):
    """Unregister an agent from A2A"""
    a2a_manager.unregister_agent(agent_id)
    return {"status": "unregistered", "agent_id": agent_id}
