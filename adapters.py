"""
Event adapters for different communication protocols.
Handles WebSocket, Webhook, MCP, and A2A connections.

IMPORTANT DESIGN NOTE:
- WebSocket adapter: Works for browser UI and persistent agents (daemons).
- Webhook adapter: Works for agents that expose an HTTP endpoint.
- MCP adapter: Events are persisted to DB for polling (MCP stdio cannot push).
- A2A adapter: Uses the event bus to route messages between registered agents.

For ephemeral CLI agents (Claude Code, Codex, OpenCode, Gemini CLI),
only MCP (polling) is viable. WebSocket and Webhook require persistent
listeners that CLI processes cannot maintain.
"""

import asyncio
import json
import logging
from typing import Dict, Set, Optional, Any
from datetime import UTC, datetime

import httpx

from event_bus import event_bus, EventType
from websocket_manager import manager as ws_manager
from database import async_session_maker
from models import AgentConnection, ProtocolType
from sqlalchemy import select

logger = logging.getLogger(__name__)


class WebSocketAdapter:
    """Manages WebSocket connections for agents and browser UI clients."""

    def __init__(self):
        self._connections: Dict[int, Set] = {}  # entity_id -> set of websockets

    async def connect(self, entity_id: int, websocket):
        """Register a WebSocket connection for an agent"""
        if entity_id not in self._connections:
            self._connections[entity_id] = set()
        self._connections[entity_id].add(websocket)

        # Update connection status in DB
        try:
            async with async_session_maker() as session:
                result = await session.execute(
                    select(AgentConnection).filter(
                        AgentConnection.entity_id == entity_id,
                        AgentConnection.protocol == ProtocolType.WEBSOCKET
                    )
                )
                conn = result.scalar_one_or_none()
                if conn:
                    conn.status = "online"
                    conn.last_seen = datetime.now(UTC)
                    await session.commit()
        except Exception as e:
            logger.error(f"Error updating WS connection status: {e}")

    def disconnect(self, entity_id: int, websocket):
        """Remove a WebSocket connection"""
        if entity_id in self._connections:
            self._connections[entity_id].discard(websocket)
            if not self._connections[entity_id]:
                del self._connections[entity_id]

    async def send_to_agent(self, entity_id: int, message: dict):
        """Send a message to all WebSocket connections for an agent"""
        if entity_id not in self._connections:
            return

        disconnected = set()
        for ws in self._connections[entity_id]:
            try:
                await ws.send_json(message)
            except Exception:
                disconnected.add(ws)

        # Clean up disconnected
        for ws in disconnected:
            self.disconnect(entity_id, ws)


class WebhookAdapter:
    """Handles webhook notifications for agents that expose HTTP endpoints."""

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def send_webhook(self, webhook_url: str, payload: dict):
        """Send a POST request to a webhook URL"""
        try:
            client = await self._get_client()
            response = await client.post(
                webhook_url,
                json=payload,
                headers={"Content-Type": "application/json", "User-Agent": "KanbanPM-Webhook/1.0"}
            )
            if response.status_code >= 400:
                logger.warning(f"Webhook failed with status {response.status_code}: {webhook_url}")
        except Exception as e:
            logger.error(f"Error sending webhook to {webhook_url}: {e}")

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()


# Global adapter instances
ws_adapter = WebSocketAdapter()
webhook_adapter = WebhookAdapter()


async def handle_event_for_adapters(event: dict):
    """
    Handle incoming events and dispatch to appropriate adapters.
    This is called by the event bus for each published event.
    """
    event_type = event.get("event_type")
    project_id = event.get("project_id")

    async with async_session_maker() as session:
        # Get all agent connections that should receive this event
        result = await session.execute(
            select(AgentConnection).filter(
                AgentConnection.status == "online"
            )
        )
        connections = result.scalars().all()

        for conn in connections:
            try:
                # Check subscription
                subscribed_events = json.loads(conn.subscribed_events or "[]")
                subscribed_projects = json.loads(conn.subscribed_projects or "null")

                # Skip if not subscribed to this event ("*" means all)
                if event_type not in subscribed_events and EventType.ALL.value not in subscribed_events:
                    continue

                # Skip if project filter doesn't match (null = all projects)
                if subscribed_projects and project_id not in subscribed_projects:
                    continue

                # Dispatch based on protocol
                if conn.protocol == ProtocolType.WEBSOCKET:
                    await ws_adapter.send_to_agent(conn.entity_id, event)

                elif conn.protocol == ProtocolType.WEBHOOK:
                    config = json.loads(conn.config or "{}")
                    webhook_url = config.get("webhook_url")
                    if webhook_url:
                        await webhook_adapter.send_webhook(webhook_url, event)

                # MCP events are handled by _persist_for_agents in event_bus.py
                # A2A events are handled by a2a.py router

            except Exception as e:
                logger.error(f"Error dispatching event to agent {conn.entity_id}: {e}")


def register_adapters():
    """
    Register adapters with the event bus.
    Called during application startup.
    """
    event_bus.set_websocket_manager(ws_manager)
    # Register for ALL events using the wildcard
    event_bus.subscribe(EventType.ALL.value, handle_event_for_adapters)
    logger.info("Event adapters registered successfully")
