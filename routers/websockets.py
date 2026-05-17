from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import logging
from websocket_manager import manager

logger = logging.getLogger(__name__)
router = APIRouter(tags=["websockets"])

@router.websocket("/ws/projects/{project_id}")
async def websocket_project_updates(websocket: WebSocket, project_id: int):
    """WebSocket endpoint for real-time project updates"""
    await manager.connect(websocket, project_id)
    try:
        await manager.send_personal_message(
            {"type": "connection", "message": f"Connected to project {project_id}"},
            websocket
        )
        
        while True:
            data = await websocket.receive_text()
            await manager.send_personal_message(
                {"type": "echo", "message": data},
                websocket
            )
    except WebSocketDisconnect:
        manager.disconnect(websocket, project_id)
    except Exception as e:
        logger.warning("WebSocket error: %s", e)
        manager.disconnect(websocket, project_id)


@router.websocket("/ws")
async def websocket_global_updates(websocket: WebSocket):
    """WebSocket endpoint for global updates"""
    await manager.connect(websocket)
    try:
        await manager.send_personal_message(
            {"type": "connection", "message": "Connected to global updates"},
            websocket
        )
        
        while True:
            data = await websocket.receive_text()
            await manager.send_personal_message(
                {"type": "echo", "message": data},
                websocket
            )
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.warning("WebSocket error: %s", e)
        manager.disconnect(websocket)

