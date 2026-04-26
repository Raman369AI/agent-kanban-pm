import json
import logging
import httpx
import asyncio
from typing import Dict, Set, List, Any, Optional
from fastapi import WebSocket
from event_bus import event_bus, EventType
from websocket_manager import manager as ui_manager

logger = logging.getLogger(__name__)

class BaseAdapter:
    async def handle_event(self, event: Dict[str, Any]):
        raise NotImplementedError

class WebSocketAdapter(BaseAdapter):
    """Dispatches events to connected agents via WebSocket"""
    def __init__(self):
        # entity_id -> Set[WebSocket]
        self.agent_connections: Dict[int, Set[WebSocket]] = {}

    async def connect(self, entity_id: int, websocket: WebSocket):
        if entity_id not in self.agent_connections:
            self.agent_connections[entity_id] = set()
        self.agent_connections[entity_id].add(websocket)
        logger.info(f"Agent {entity_id} connected via WebSocket")

    def disconnect(self, entity_id: int, websocket: WebSocket):
        if entity_id in self.agent_connections:
            self.agent_connections[entity_id].discard(websocket)
            if not self.agent_connections[entity_id]:
                del self.agent_connections[entity_id]
        logger.info(f"Agent {entity_id} disconnected from WebSocket")

    async def handle_event(self, event: Dict[str, Any]):
        # Broadcast to UI clients first (legacy compatibility)
        project_id = event.get("project_id")
        if project_id:
            await ui_manager.broadcast_to_project(event, project_id)
        else:
            await ui_manager.broadcast_to_all(event)

        # Dispatch to specific agents if they are subscribed (handled by filter in manage_dispatch)
        # For now, we'll just broadcast to all connected agents for simplicity, 
        # but in a real system we'd check their subscriptions.
        
        disconnected_agents = []
        for eid, sockets in self.agent_connections.items():
            disconnected_sockets = set()
            for ws in sockets:
                try:
                    await ws.send_json(event)
                except Exception as e:
                    logger.error(f"Error sending event to agent {eid}: {e}")
                    disconnected_sockets.add(ws)
            
            for ws in disconnected_sockets:
                sockets.discard(ws)
            
            if not sockets:
                disconnected_agents.append(eid)
        
        for eid in disconnected_agents:
            if eid in self.agent_connections:
                del self.agent_connections[eid]

class WebhookAdapter(BaseAdapter):
    """Dispatches events via HTTP POST to registered webhook URLs"""
    def __init__(self, db_session_factory):
        self.session_factory = db_session_factory

    async def handle_event(self, event: Dict[str, Any]):
        # This will be called by manage_dispatch which already does the filtering
        # We need to find the specific connection config for this event
        pass

    async def send_webhook(self, url: str, event: Dict[str, Any]):
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(url, json=event, timeout=5.0)
                response.raise_for_status()
                logger.info(f"Successfully sent webhook to {url}")
            except Exception as e:
                logger.error(f"Webhook delivery failed to {url}: {e}")

class A2AAdapter(BaseAdapter):
    """Dispatches events via Agent-to-Agent protocol"""
    async def handle_event(self, event: Dict[str, Any]):
        # Placeholder for A2A implementation
        logger.info(f"A2A dispatch for {event['event_type']}")

class MCPAdapter(BaseAdapter):
    """Manages events for MCP-connected agents (pull-based)"""
    def __init__(self):
        # entity_id -> List[event]
        self.event_queues: Dict[int, List[Dict[str, Any]]] = {}

    async def handle_event(self, event: Dict[str, Any]):
        # This will be filtered and added to queues by the central dispatcher
        pass

    def add_to_queue(self, entity_id: int, event: Dict[str, Any]):
        if entity_id not in self.event_queues:
            self.event_queues[entity_id] = []
        self.event_queues[entity_id].append(event)
        # Keep queue size reasonable
        if len(self.event_queues[entity_id]) > 100:
            self.event_queues[entity_id].pop(0)

    def get_events(self, entity_id: int) -> List[Dict[str, Any]]:
        events = self.event_queues.get(entity_id, [])
        self.event_queues[entity_id] = []
        return events

# Global adapter instances
ws_adapter = WebSocketAdapter()
webhook_adapter = WebhookAdapter(None)  # session_factory set lazily via manage_dispatch
a2a_adapter = A2AAdapter()
mcp_adapter = MCPAdapter()

async def manage_dispatch(event: Dict[str, Any]):
    """Central dispatcher that filters and routes events to correct adapters/agents.

    Persists events to the database (pending_events table) so that both the
    FastAPI server and external MCP server processes can read them.
    """
    from database import async_session_maker
    from models import AgentConnection, ProtocolType, PendingEvent
    from sqlalchemy import select
    import json

    event_type = event["event_type"]
    project_id = event.get("project_id")

    # Always broadcast to UI via WebSocket adapter
    await ws_adapter.handle_event(event)

    # Find agent connections that match this event
    async with async_session_maker() as db:
        try:
            result = await db.execute(select(AgentConnection))
            connections = result.scalars().all()
            for conn in connections:
                # Check subscriptions
                try:
                    subs = json.loads(conn.subscribed_events) if conn.subscribed_events else []
                    projects = json.loads(conn.subscribed_projects) if conn.subscribed_projects else None
                except:
                    continue

                if event_type not in subs:
                    continue

                if projects is not None and project_id not in projects:
                    continue

                # Dispatch based on protocol
                if conn.protocol == ProtocolType.WEBHOOK:
                    try:
                        config = json.loads(conn.config) if conn.config else {}
                        url = config.get("webhook_url")
                        if url:
                            asyncio.create_task(webhook_adapter.send_webhook(url, event))
                    except:
                        continue

                elif conn.protocol == ProtocolType.A2A:
                    await a2a_adapter.handle_event(event)

                elif conn.protocol == ProtocolType.WEBSOCKET:
                    # Already handled by ws_adapter if they are connected
                    pass

                # Always persist to DB for MCP/polling agents (works cross-process)
                pending = PendingEvent(
                    agent_id=conn.entity_id,
                    event_type=event_type,
                    payload=json.dumps(event),
                    project_id=project_id
                )
                db.add(pending)

            await db.commit()
        except Exception as e:
            logger.error(f"Error in manage_dispatch: {e}", exc_info=True)

def register_adapters():
    """Register the central dispatcher with the event bus"""
    for event_type in EventType:
        event_bus.subscribe(event_type.value, manage_dispatch)
