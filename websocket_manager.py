from fastapi import WebSocket
from typing import Dict, Set, List
import json
from datetime import datetime


class ConnectionManager:
    """Manages WebSocket connections for real-time updates"""
    
    def __init__(self):
        # Store connections by project_id
        self.active_connections: Dict[int, Set[WebSocket]] = {}
        # Store all connections
        self.all_connections: Set[WebSocket] = set()
    
    async def connect(self, websocket: WebSocket, project_id: int = None):
        """Accept a new WebSocket connection"""
        await websocket.accept()
        self.all_connections.add(websocket)
        
        if project_id:
            if project_id not in self.active_connections:
                self.active_connections[project_id] = set()
            self.active_connections[project_id].add(websocket)
    
    def disconnect(self, websocket: WebSocket, project_id: int = None):
        """Remove a WebSocket connection"""
        self.all_connections.discard(websocket)
        
        if project_id and project_id in self.active_connections:
            self.active_connections[project_id].discard(websocket)
            # Clean up empty project rooms
            if not self.active_connections[project_id]:
                del self.active_connections[project_id]
    
    async def send_personal_message(self, message: dict, websocket: WebSocket):
        """Send a message to a specific connection"""
        try:
            await websocket.send_json(message)
        except Exception as e:
            print(f"Error sending personal message: {e}")
    
    async def broadcast_to_project(self, message: dict, project_id: int):
        """Broadcast a message to all connections watching a project"""
        if project_id not in self.active_connections:
            return
        
        disconnected = set()
        for connection in self.active_connections[project_id]:
            try:
                await connection.send_json(message)
            except Exception as e:
                print(f"Error broadcasting to project {project_id}: {e}")
                disconnected.add(connection)
        
        # Clean up disconnected connections
        for connection in disconnected:
            self.disconnect(connection, project_id)
    
    async def broadcast_to_all(self, message: dict):
        """Broadcast a message to all connections"""
        disconnected = set()
        for connection in self.all_connections:
            try:
                await connection.send_json(message)
            except Exception as e:
                print(f"Error broadcasting to all: {e}")
                disconnected.add(connection)
        
        # Clean up disconnected connections
        for connection in disconnected:
            self.disconnect(connection)


# Global instance
manager = ConnectionManager()


def create_notification(event_type: str, data: dict, project_id: int = None):
    """Create a standardized notification message"""
    return {
        "event_type": event_type,
        "timestamp": datetime.utcnow().isoformat(),
        "project_id": project_id,
        "data": data
    }
