"""
Agent Workers - Background polling workers that act as real agents.

Each registered agent gets a background async loop that:
1. Polls GET /agents/events via HTTP (same path external agents use)
2. Processes events and takes actions via HTTP API calls
3. Runs independently — can be stopped/started per agent

This is NOT the server faking comments. These are real HTTP-client workers
that go through the same API as Gemini CLI or any external agent.
"""

import asyncio
import logging
import json
from datetime import datetime
from typing import Dict, Any, Optional

import httpx
from sqlalchemy import select

from database import async_session_maker
from models import Entity, EntityType

logger = logging.getLogger("agent_workers")

BASE_URL = "http://127.0.0.1:8000"

# Track running workers so we can stop them
_running_workers: Dict[int, asyncio.Task] = {}


class AgentWorker:
    """A real background agent that polls events via HTTP and takes actions via API."""

    def __init__(self, agent_id: int, agent_name: str, api_key: str):
        self.agent_id = agent_id
        self.name = agent_name
        self.api_key = api_key
        self.headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
        self._running = False

    def log(self, msg: str):
        logger.info(f"[{self.name}] {msg}")
        print(f"[AGENT:{self.name}] {msg}")

    async def poll_events(self) -> list:
        """Poll for pending events via HTTP — same as any external agent."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{BASE_URL}/agents/events",
                    headers=self.headers,
                    timeout=5.0
                )
                if resp.status_code == 200:
                    return resp.json().get("events", [])
                else:
                    self.log(f"Poll failed: {resp.status_code}")
        except Exception as e:
            self.log(f"Poll error: {e}")
        return []

    async def add_comment(self, task_id: int, content: str) -> bool:
        """Post a comment via HTTP API."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{BASE_URL}/comments",
                    headers=self.headers,
                    json={"task_id": task_id, "content": content},
                    timeout=5.0
                )
                return resp.status_code == 201
        except Exception as e:
            self.log(f"Comment error: {e}")
            return False

    async def self_assign(self, task_id: int) -> bool:
        """Self-assign to a task via HTTP API."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{BASE_URL}/tasks/{task_id}/self-assign",
                    headers=self.headers,
                    json={"entity_id": self.agent_id},
                    timeout=5.0
                )
                return resp.status_code == 200
        except Exception as e:
            self.log(f"Self-assign error: {e}")
            return False

    async def move_task(self, task_id: int, stage_id: int, status: str) -> bool:
        """Move a task via HTTP API."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.patch(
                    f"{BASE_URL}/ui/tasks/{task_id}/move",
                    headers=self.headers,
                    json={"stage_id": stage_id, "status": status},
                    timeout=5.0
                )
                return resp.status_code == 200
        except Exception as e:
            self.log(f"Move task error: {e}")
            return False

    async def get_task(self, task_id: int) -> Optional[dict]:
        """Fetch task details via HTTP API."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{BASE_URL}/tasks/{task_id}",
                    headers=self.headers,
                    timeout=5.0
                )
                if resp.status_code == 200:
                    return resp.json()
        except Exception as e:
            self.log(f"Get task error: {e}")
        return None

    # ---- Event Handlers ----

    async def handle_task_created(self, data: dict):
        task_id = data.get("task_id")
        title = data.get("title", "")
        self.log(f"NEW TASK #{task_id}: \"{title}\" — standing by for assignment")

    async def handle_task_assigned(self, data: dict):
        task_id = data.get("task_id")
        entity_id = data.get("entity_id")

        if entity_id != self.agent_id:
            return  # Not assigned to me

        task = await self.get_task(task_id)
        if not task:
            return

        self.log(f"ASSIGNED to Task #{task_id}: \"{task['title']}\"")
        await self.add_comment(
            task_id,
            f"[{self.name}] I've been assigned to this task. "
            f"Reviewing requirements for: \"{task['title']}\""
        )

    async def handle_task_moved(self, data: dict):
        task_id = data.get("task_id")
        title = data.get("title", "")
        new_status = data.get("status", "")

        # Check if I'm assigned to this task
        task = await self.get_task(task_id)
        if not task:
            return

        my_assignee = any(
            a["id"] == self.agent_id
            for a in task.get("assignees", [])
        )

        if new_status == "in_progress":
            if my_assignee:
                self.log(f"WORKING on Task #{task_id}: \"{title}\"")
                await self.add_comment(
                    task_id,
                    f"[{self.name}] Task is now In Progress. "
                    f"I'm actively working on: \"{title}\""
                )
            else:
                # Unassigned task moved to in_progress — volunteer
                self.log(f"VOLUNTEERING for unassigned Task #{task_id}: \"{title}\"")
                if await self.self_assign(task_id):
                    self.log(f"Self-assigned to Task #{task_id}")
                    await self.add_comment(
                        task_id,
                        f"[{self.name}] Auto-assigned and starting work on: \"{title}\""
                    )

        elif new_status == "in_review" and my_assignee:
            self.log(f"REVIEW requested for Task #{task_id}: \"{title}\"")
            await self.add_comment(
                task_id,
                f"[{self.name}] Task submitted for review: \"{title}\""
            )

        elif new_status == "completed" and my_assignee:
            self.log(f"COMPLETED Task #{task_id}: \"{title}\"")

        elif new_status == "blocked" and my_assignee:
            self.log(f"BLOCKED Task #{task_id}: \"{title}\" — waiting for resolution")
            await self.add_comment(
                task_id,
                f"[{self.name}] Task is blocked. Waiting for resolution: \"{title}\""
            )

    # ---- Main Loop ----

    async def run(self):
        """Main polling loop — runs until cancelled."""
        self._running = True
        self.log(f"Worker started (id={self.agent_id}) — polling {BASE_URL}")

        while self._running:
            try:
                events = await self.poll_events()
                for event in events:
                    event_type = event.get("event_type")
                    data = event.get("data") or event.get("payload", {})

                    if event_type == "task_created":
                        await self.handle_task_created(data)
                    elif event_type == "task_assigned":
                        await self.handle_task_assigned(data)
                    elif event_type == "task_moved":
                        await self.handle_task_moved(data)
                    elif event_type == "task_updated":
                        self.log(f"Task #{data.get('task_id')} updated: {data.get('status')}")

            except asyncio.CancelledError:
                self.log("Worker cancelled")
                break
            except Exception as e:
                self.log(f"Loop error: {e}")

            await asyncio.sleep(3)

        self.log("Worker stopped")

    def stop(self):
        self._running = False


async def start_agent_workers():
    """Start a background worker for every registered agent.
    Called after the server is fully ready to accept HTTP requests.
    """
    # Small delay to let uvicorn bind the port
    await asyncio.sleep(2)

    async with async_session_maker() as db:
        result = await db.execute(
            select(Entity).filter(
                Entity.entity_type == EntityType.AGENT,
                Entity.is_active == True,
                Entity.api_key.isnot(None)
            )
        )
        agents = result.scalars().all()

    if not agents:
        logger.info("No agents found — no workers to start")
        return

    for agent in agents:
        worker = AgentWorker(agent.id, agent.name, agent.api_key)
        task = asyncio.create_task(worker.run())
        _running_workers[agent.id] = task
        logger.info(f"Started worker for agent '{agent.name}' (id={agent.id})")

    print(f"\n{'='*60}")
    print(f"  AGENT WORKERS: {len(agents)} agents polling for events")
    for a in agents:
        print(f"    - {a.name} (id={a.id})")
    print(f"{'='*60}\n")


def register_agent_reactor():
    """Schedule agent workers to start after the server is ready."""
    asyncio.ensure_future(start_agent_workers())
    logger.info("Agent workers scheduled to start")
