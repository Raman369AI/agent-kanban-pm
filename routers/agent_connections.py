from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List
import json
import asyncio
from datetime import datetime

from database import get_db
from models import AgentConnection, Entity, ProtocolType, ConnectionStatus, PendingEvent
from schemas import AgentConnectionCreate, AgentConnectionResponse
from auth import get_current_agent
from event_bus import event_bus, EventType

router = APIRouter(prefix="/agents", tags=["Agent Connections"])


@router.post("/connect", response_model=AgentConnectionResponse)
async def register_connection(
    connection_in: AgentConnectionCreate,
    db: AsyncSession = Depends(get_db),
    current_agent: Entity = Depends(get_current_agent)
):
    """Register or update an agent's connection preferences"""
    connection = AgentConnection(
        entity_id=current_agent.id,
        protocol=connection_in.protocol,
        config=json.dumps(connection_in.config),
        subscribed_events=json.dumps(connection_in.subscribed_events),
        subscribed_projects=json.dumps(connection_in.subscribed_projects) if connection_in.subscribed_projects else None,
        status=ConnectionStatus.ONLINE if connection_in.protocol == "websocket" else ConnectionStatus.OFFLINE,
        last_seen=datetime.utcnow()
    )

    db.add(connection)
    await db.commit()
    await db.refresh(connection)

    return {
        "id": connection.id,
        "entity_id": connection.entity_id,
        "protocol": connection.protocol,
        "config": connection_in.config,
        "subscribed_events": connection_in.subscribed_events,
        "subscribed_projects": connection_in.subscribed_projects,
        "status": connection.status,
        "last_seen": connection.last_seen
    }


@router.get("/connections", response_model=List[AgentConnectionResponse])
async def get_connections(
    db: AsyncSession = Depends(get_db),
    current_agent: Entity = Depends(get_current_agent)
):
    """List all connection preferences for the current agent"""
    result = await db.execute(
        select(AgentConnection).filter(AgentConnection.entity_id == current_agent.id)
    )
    connections = result.scalars().all()

    out = []
    for conn in connections:
        out.append({
            "id": conn.id,
            "entity_id": conn.entity_id,
            "protocol": conn.protocol,
            "config": json.loads(conn.config) if conn.config else {},
            "subscribed_events": json.loads(conn.subscribed_events) if conn.subscribed_events else [],
            "subscribed_projects": json.loads(conn.subscribed_projects) if conn.subscribed_projects else None,
            "status": conn.status,
            "last_seen": conn.last_seen
        })
    return out


@router.patch("/connections/{connection_id}", response_model=AgentConnectionResponse)
async def update_connection(
    connection_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_agent: Entity = Depends(get_current_agent)
):
    """Update an agent connection's subscriptions or config"""
    result = await db.execute(
        select(AgentConnection).filter(
            AgentConnection.id == connection_id,
            AgentConnection.entity_id == current_agent.id
        )
    )
    conn = result.scalar_one_or_none()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")

    body = await request.json()

    if "subscribed_events" in body:
        conn.subscribed_events = json.dumps(body["subscribed_events"])
    if "subscribed_projects" in body:
        conn.subscribed_projects = json.dumps(body["subscribed_projects"]) if body["subscribed_projects"] else None
    if "config" in body:
        conn.config = json.dumps(body["config"])

    conn.last_seen = datetime.utcnow()
    await db.commit()
    await db.refresh(conn)

    return {
        "id": conn.id,
        "entity_id": conn.entity_id,
        "protocol": conn.protocol,
        "config": json.loads(conn.config) if conn.config else {},
        "subscribed_events": json.loads(conn.subscribed_events) if conn.subscribed_events else [],
        "subscribed_projects": json.loads(conn.subscribed_projects) if conn.subscribed_projects else None,
        "status": conn.status,
        "last_seen": conn.last_seen
    }


@router.delete("/disconnect")
async def disconnect_agent(
    db: AsyncSession = Depends(get_db),
    current_agent: Entity = Depends(get_current_agent)
):
    """Revoke all connection preferences for the agent"""
    result = await db.execute(
        select(AgentConnection).filter(AgentConnection.entity_id == current_agent.id)
    )
    connections = result.scalars().all()
    for conn in connections:
        await db.delete(conn)
    await db.commit()
    return {"message": "All agent connections revoked"}


@router.get("/events")
async def agent_event_stream(
    db: AsyncSession = Depends(get_db),
    current_agent: Entity = Depends(get_current_agent)
):
    """Returns pending events for the agent from the database queue."""

    # Update connection status
    result = await db.execute(
        select(AgentConnection).filter(AgentConnection.entity_id == current_agent.id)
    )
    connections = result.scalars().all()
    for conn in connections:
        conn.status = ConnectionStatus.ONLINE
        conn.last_seen = datetime.utcnow()

    # Read pending events from DB
    evt_result = await db.execute(
        select(PendingEvent)
        .filter(PendingEvent.agent_id == current_agent.id)
        .order_by(PendingEvent.created_at.asc())
    )
    pending = evt_result.scalars().all()

    events = []
    for pe in pending:
        try:
            events.append(json.loads(pe.payload))
        except Exception:
            pass
        await db.delete(pe)

    await db.commit()
    return {"agent_id": current_agent.id, "events": events}
