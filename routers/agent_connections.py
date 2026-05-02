"""
Agent Connection Management
Endpoints for managing how agents connect to the system.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List, Optional
from datetime import UTC, datetime
import json
import logging

from database import get_db
from models import Entity, EntityType, AgentConnection, ProtocolType, ConnectionStatus
from schemas import AgentConnectionCreate, AgentConnectionResponse
from auth import get_current_entity, require_worker, require_manager, is_owner_or_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/agent-connections", tags=["agent-connections"])


@router.get("", response_model=List[AgentConnectionResponse])
async def list_connections(
    entity_id: Optional[int] = None,
    protocol: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_entity: Entity = Depends(require_worker),
):
    """List all agent connections. Requires WORKER+ role."""
    query = select(AgentConnection)

    if entity_id:
        query = query.filter(AgentConnection.entity_id == entity_id)
    if protocol:
        query = query.filter(AgentConnection.protocol == protocol)

    result = await db.execute(query)
    connections = result.scalars().all()

    return [
        AgentConnectionResponse(
            id=c.id,
            entity_id=c.entity_id,
            protocol=c.protocol.value if hasattr(c.protocol, 'value') else str(c.protocol),
            config=json.loads(c.config or "{}"),
            subscribed_events=json.loads(c.subscribed_events or "[]"),
            subscribed_projects=json.loads(c.subscribed_projects or "null"),
            status=c.status.value if hasattr(c.status, 'value') else str(c.status),
            last_seen=c.last_seen
        )
        for c in connections
    ]


@router.post("", response_model=AgentConnectionResponse, status_code=status.HTTP_201_CREATED)
async def create_connection(
    connection: AgentConnectionCreate,
    entity_id: int,
    db: AsyncSession = Depends(get_db),
    current_entity: Entity = Depends(require_manager),
):
    """Create a new agent connection. Requires MANAGER+ role."""
    result = await db.execute(
        select(Entity).filter(
            Entity.id == entity_id,
            Entity.entity_type == EntityType.AGENT
        )
    )
    entity = result.scalar_one_or_none()

    if not entity:
        raise HTTPException(status_code=404, detail="Agent not found")

    protocol_map = {
        "websocket": ProtocolType.WEBSOCKET,
        "webhook": ProtocolType.WEBHOOK,
        "mcp": ProtocolType.MCP,
        "a2a": ProtocolType.A2A
    }

    protocol_enum = protocol_map.get(connection.protocol.lower())
    if not protocol_enum:
        raise HTTPException(status_code=400, detail=f"Invalid protocol: {connection.protocol}")

    db_connection = AgentConnection(
        entity_id=entity_id,
        protocol=protocol_enum,
        config=json.dumps(connection.config),
        subscribed_events=json.dumps(connection.subscribed_events),
        subscribed_projects=json.dumps(connection.subscribed_projects) if connection.subscribed_projects else None,
        status=ConnectionStatus.ONLINE,
        last_seen=datetime.now(UTC)
    )

    db.add(db_connection)
    await db.commit()
    await db.refresh(db_connection)

    logger.info(f"Agent connection created: {entity.name} -> {connection.protocol}")

    return AgentConnectionResponse(
        id=db_connection.id,
        entity_id=db_connection.entity_id,
        protocol=db_connection.protocol.value,
        config=connection.config,
        subscribed_events=connection.subscribed_events,
        subscribed_projects=connection.subscribed_projects,
        status=db_connection.status.value,
        last_seen=db_connection.last_seen
    )


@router.get("/{connection_id}", response_model=AgentConnectionResponse)
async def get_connection(
    connection_id: int,
    db: AsyncSession = Depends(get_db),
    current_entity: Entity = Depends(require_worker),
):
    """Get a specific agent connection. Requires WORKER+ role."""
    result = await db.execute(
        select(AgentConnection).filter(AgentConnection.id == connection_id)
    )
    c = result.scalar_one_or_none()

    if not c:
        raise HTTPException(status_code=404, detail="Connection not found")

    return AgentConnectionResponse(
        id=c.id,
        entity_id=c.entity_id,
        protocol=c.protocol.value if hasattr(c.protocol, 'value') else str(c.protocol),
        config=json.loads(c.config or "{}"),
        subscribed_events=json.loads(c.subscribed_events or "[]"),
        subscribed_projects=json.loads(c.subscribed_projects or "null"),
        status=c.status.value if hasattr(c.status, 'value') else str(c.status),
        last_seen=c.last_seen
    )


@router.patch("/{connection_id}", response_model=AgentConnectionResponse)
async def update_connection(
    connection_id: int,
    subscribed_events: Optional[List[str]] = None,
    subscribed_projects: Optional[List[int]] = None,
    config: Optional[dict] = None,
    db: AsyncSession = Depends(get_db),
    current_entity: Entity = Depends(require_manager),
):
    """Update an agent connection. Requires MANAGER+ role."""
    result = await db.execute(
        select(AgentConnection).filter(AgentConnection.id == connection_id)
    )
    connection = result.scalar_one_or_none()

    if not connection:
        raise HTTPException(status_code=404, detail="Connection not found")

    if subscribed_events is not None:
        connection.subscribed_events = json.dumps(subscribed_events)
    if subscribed_projects is not None:
        connection.subscribed_projects = json.dumps(subscribed_projects)
    if config is not None:
        connection.config = json.dumps(config)

    connection.last_seen = datetime.now(UTC)
    await db.commit()
    await db.refresh(connection)

    return AgentConnectionResponse(
        id=connection.id,
        entity_id=connection.entity_id,
        protocol=connection.protocol.value if hasattr(connection.protocol, 'value') else str(connection.protocol),
        config=json.loads(connection.config or "{}"),
        subscribed_events=json.loads(connection.subscribed_events or "[]"),
        subscribed_projects=json.loads(connection.subscribed_projects or "null"),
        status=connection.status.value if hasattr(connection.status, 'value') else str(connection.status),
        last_seen=connection.last_seen
    )


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connection(
    connection_id: int,
    db: AsyncSession = Depends(get_db),
    current_entity: Entity = Depends(require_manager),
):
    """Delete an agent connection. Requires MANAGER+ role."""
    result = await db.execute(
        select(AgentConnection).filter(AgentConnection.id == connection_id)
    )
    connection = result.scalar_one_or_none()

    if not connection:
        raise HTTPException(status_code=404, detail="Connection not found")

    await db.delete(connection)
    await db.commit()


@router.post("/{connection_id}/heartbeat")
async def connection_heartbeat(
    connection_id: int,
    db: AsyncSession = Depends(get_db),
    current_entity: Entity = Depends(require_worker),
):
    """Update the last_seen timestamp for a connection. Requires WORKER+ role."""
    result = await db.execute(
        select(AgentConnection).filter(AgentConnection.id == connection_id)
    )
    connection = result.scalar_one_or_none()

    if not connection:
        raise HTTPException(status_code=404, detail="Connection not found")

    connection.last_seen = datetime.now(UTC)
    connection.status = ConnectionStatus.ONLINE
    await db.commit()

    return {"ok": True, "last_seen": connection.last_seen.isoformat()}
