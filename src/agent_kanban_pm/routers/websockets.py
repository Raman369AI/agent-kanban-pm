from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import logging
from agent_kanban_pm.ws import manager

logger = logging.getLogger(__name__)
router = APIRouter(tags=["websockets"])


def verify_ws_token(websocket: WebSocket) -> bool:
    import os
    if os.getenv("KANBAN_TESTING") == "1":
        return True

    from agent_kanban_pm.runtime.instance import get_auth_token
    expected_token = get_auth_token()

    token = (
        websocket.headers.get("x-kanban-token")
        or websocket.cookies.get("kanban-token")
        or websocket.query_params.get("token")
        or websocket.query_params.get("kanban-token")
    )

    if not token:
        auth_header = websocket.headers.get("authorization")
        if auth_header:
            if auth_header.lower().startswith("bearer "):
                token = auth_header[7:]
            else:
                token = auth_header

    return token == expected_token


@router.websocket("/ws/projects/{project_id}")
async def websocket_project_updates(websocket: WebSocket, project_id: int):
    """WebSocket endpoint for real-time project updates"""
    if not verify_ws_token(websocket):
        await websocket.accept()
        await websocket.close(code=4401, reason="Unauthorized")
        return
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
    if not verify_ws_token(websocket):
        await websocket.accept()
        await websocket.close(code=4401, reason="Unauthorized")
        return
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

