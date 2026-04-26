"""
Event adapters for different communication protocols.
Handles WebSocket, Webhook, MCP, and A2A connections.
"""

import asyncio
import json
import aiohttp
from typing import Dict, Set, Optional, Any
from datetime import datetime

from event_bus import event_bus, EventType
from websocket_manager import manager as ws_manager
from database import async_session_maker
from models import AgentConnection, ProtocolType
from sqlalchemy import select


class WebSocketAdapter:
    """Manages WebSocket connections for agents"""
    
    def __init__(self):
        self._connections: Dict[int, Set] = {}  # entity_id -> set of websockets
    
    async def connect(self, entity_id: int, websocket):
        """Register a WebSocket connection for an agent"""
        if entity_id not in self._connections:
            self._connections[entity_id] = set()
        self._connections[entity_id].add(websocket)
        
        # Update connection status
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
                conn.last_seen = datetime.utcnow()
                await session.commit()
    
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
    """Handles webhook notifications for agents"""
    
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
    
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session
    
    async def send_webhook(self, webhook_url: str, payload: dict):
        """Send a POST request to a webhook URL"""
        try:
            session = await self._get_session()
            async with session.post(
                webhook_url, 
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status >= 400:
                    print(f"Webhook failed with status {response.status}")
        except Exception as e:
            print(f"Error sending webhook: {e}")
    
    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# Global adapter instances
ws_adapter = WebSocketAdapter()
webhook_adapter = WebhookAdapter()


async def handle_event_for_adapters(event: dict):
    """
    Handle incoming events and dispatch to appropriate adapters.
    This is called by the event bus for each published event.
    """
    event_type = event.get("event_type")
    
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
                
                if event_type not in subscribed_events:
                    continue
                
                project_id = event.get("project_id")
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
                        
            except Exception as e:
                print(f"Error dispatching event to agent {conn.entity_id}: {e}")


def register_adapters():
    """
    Register adapters with the event bus.
    Called during application startup.
    """
    event_bus.set_websocket_manager(ws_manager)
    event_bus.subscribe("*", handle_event_for_adapters)
    print("Event adapters registered")
