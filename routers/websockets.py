from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from websocket_manager import manager

router = APIRouter(tags=["websockets"])

@router.websocket("/ws/projects/{project_id}")
async def websocket_project_updates(websocket: WebSocket, project_id: int):
    """WebSocket endpoint for real-time project updates"""
    await manager.connect(websocket, project_id)
    try:
        # Send initial connection message
        await manager.send_personal_message(
            {"type": "connection", "message": f"Connected to project {project_id}"},
            websocket
        )
        
        # Keep connection alive and listen for messages
        while True:
            data = await websocket.receive_text()
            # Echo back or handle specific commands if needed
            await manager.send_personal_message(
                {"type": "echo", "message": data},
                websocket
            )
    except WebSocketDisconnect:
        manager.disconnect(websocket, project_id)
    except Exception as e:
        print(f"WebSocket error: {e}")
        manager.disconnect(websocket, project_id)


@router.websocket("/ws")
async def websocket_global_updates(websocket: WebSocket):
    """WebSocket endpoint for global updates"""
    await manager.connect(websocket)
    try:
        # Send initial connection message
        await manager.send_personal_message(
            {"type": "connection", "message": "Connected to global updates"},
            websocket
        )
        
        # Keep connection alive
        while True:
            data = await websocket.receive_text()
            await manager.send_personal_message(
                {"type": "echo", "message": data},
                websocket
            )
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        print(f"WebSocket error: {e}")
        manager.disconnect(websocket)
