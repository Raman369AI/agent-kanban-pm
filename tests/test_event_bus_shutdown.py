import asyncio

import pytest

from agent_kanban_pm.events import EventBus


@pytest.mark.asyncio
async def test_stop_async_drains_queued_events(monkeypatch):
    bus = EventBus()
    handled = []

    async def subscriber(payload):
        await asyncio.sleep(0.01)
        handled.append(payload["data"]["value"])

    async def skip_persistence(event_type, payload, project_id):
        return None

    monkeypatch.setattr(bus, "_persist_for_agents", skip_persistence)
    bus.subscribe("example", subscriber)
    bus.start()
    await bus.publish("example", {"value": 42})

    await bus.stop_async()

    assert handled == [42]
    assert bus._worker_task is None
    assert bus._queue is None
