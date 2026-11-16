from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, status
from database.database import SessionDep, update_entity
from database.models import Notification, User
from schemas.notification_schemas import NotificationPublic
from services.notification_service import notification_service
from utils.bearer_auth import retrieve_user_data
from utils.validation import validate_entity_exists

router = APIRouter()

@router.put("/{id}/read", response_model=NotificationPublic)
def mark_notification_as_read(
    id: str,
    session: SessionDep,
    user_data: Annotated[User, Depends(retrieve_user_data)],
):
    notification = validate_entity_exists(session, Notification, id, "Notification")
    
    if notification.user_id != user_data.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access this notification",
        )

    return update_entity(session, notification, {"is_read": True})

@router.put("/{id}/toast-shown", response_model=NotificationPublic)
def mark_notification_as_read(
        id: str,
        session: SessionDep,
        user_data: Annotated[User, Depends(retrieve_user_data)],
):
    notification = validate_entity_exists(session, Notification, id, "Notification")

    if notification.user_id != user_data.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access this notification",
        )

    return update_entity(session, notification, {"toast_shown": True})

@router.put("/read-all", response_model=dict)
def mark_all_notifications_as_read(
    session: SessionDep,
    user_data: Annotated[User, Depends(retrieve_user_data)],
):
    notification_service.mark_all_as_read(session, user_data.id)
    return {"message": "All notifications marked as read"}
