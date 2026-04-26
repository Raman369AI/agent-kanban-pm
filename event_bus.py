import asyncio
import logging
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
from enum import Enum

logger = logging.getLogger(__name__)

class EventType(str, Enum):
    TASK_CREATED = "task_created"
    TASK_UPDATED = "task_updated"
    TASK_MOVED = "task_moved"
    TASK_ASSIGNED = "task_assigned"
    TASK_UNASSIGNED = "task_unassigned"
    TASK_DELETED = "task_deleted"
    TASK_COMMENTED = "task_commented"
    PROJECT_CREATED = "project_created"
    PROJECT_UPDATED = "project_updated"
    PROJECT_DELETED = "project_deleted"

class EventBus:
    _instance: Optional['EventBus'] = None
    _subscribers: Dict[str, List[Callable]] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(EventBus, cls).__new__(cls)
            cls._subscribers = {event_type.value: [] for event_type in EventType}
        return cls._instance

    def subscribe(self, event_type: str, callback: Callable):
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(callback)
        logger.info(f"Subscribed callback to event: {event_type}")

    async def publish(self, event_type: str, data: Any, entity_id: Optional[int] = None, project_id: Optional[int] = None):
        if event_type not in self._subscribers:
            logger.warning(f"No subscribers for event type: {event_type}")
            return

        event_payload = {
            "event_type": event_type,
            "timestamp": datetime.utcnow().isoformat(),
            "entity_id": entity_id,
            "project_id": project_id,
            "data": data,
            "payload": data  # Keep for backward compat with polling agents
        }

        tasks = []
        for callback in self._subscribers[event_type]:
            tasks.append(self._run_callback(callback, event_payload))
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            logger.info(f"Published event {event_type} to {len(tasks)} subscribers")

    async def _run_callback(self, callback: Callable, payload: Dict[str, Any]):
        try:
            if asyncio.iscoroutinefunction(callback):
                await callback(payload)
            else:
                callback(payload)
        except Exception as e:
            logger.error(f"Error in event bus callback: {e}", exc_info=True)

# Global singleton instance
event_bus = EventBus()
