from fastapi import APIRouter, WebSocket, WebSocketDisconnect

import utils.log as log
from database.database import create_session
from database.models import Notification
from schemas.notification_schemas import NotificationPublic
from utils.bearer_auth import validate_jwt
from utils.broadcaster import broadcaster


router = APIRouter()
logger = log.setup_custom_logger(__name__)


@router.websocket("/notifications")
async def get_notifications(websocket: WebSocket, token: str):
    logger.debug("Handling WebSocket connection for notifications")
    user_data = validate_jwt(token)
    if not user_data:
        logger.error("Invalid token for notifications WebSocket")
        return

    await websocket.accept()
    queue = None
    topic = None
    try:
        logger.info(f"Notification WebSocket connection for user: {user_data.id}")

        sent_notification_ids = set()
        with create_session() as session:
            notifications = (
                session.query(Notification)
                .filter_by(user_id=user_data.id, is_read=False)
                .order_by(Notification.created_at.asc())
                .all()
            )
            for notification in notifications:
                notification_data = NotificationPublic.model_validate(
                    notification
                ).model_dump_json()
                await websocket.send_text(notification_data)
                sent_notification_ids.add(notification.id)

        topic = f"notifications:{user_data.id}"
        queue = broadcaster.subscribe(topic)

        while True:
            data = await queue.get()
            if data.get("id") not in sent_notification_ids:
                notification_data = NotificationPublic.model_validate(
                    data
                ).model_dump_json()
                await websocket.send_text(notification_data)
                sent_notification_ids.add(data.get("id"))

    except WebSocketDisconnect:
        logger.info("Notification WebSocket disconnected")
    except Exception as e:
        logger.error(f"Notification WebSocket error: {e}")
        await websocket.close()
    finally:
        if queue and topic:
            broadcaster.unsubscribe(topic, queue)
