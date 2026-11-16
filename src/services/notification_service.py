import json
import asyncio

from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from database.models import Notification, UserProject
from schemas.notification_schemas import NotificationPublic
from utils.broadcaster import broadcaster
from utils import log

logger = log.setup_custom_logger(__name__)


def run_coro_safely(coro):
    """
    Runs or schedules an async coroutine from sync code in a thread-safe way.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No loop in this thread, try to find one or run a new one
        return asyncio.run(coro)

    if loop.is_running():
        return asyncio.run_coroutine_threadsafe(coro, loop)
    else:
        return loop.create_task(coro)


class NotificationService:

    def _serialize_message(self, message) -> str:
        """
        Converts notification messages to a database-safe string.

        Dict/list payloads are stored as JSON strings. Plain string messages are
        stored unchanged.
        """
        if isinstance(message, str):
            return message

        return json.dumps(message, default=str, ensure_ascii=False)

    def notify_user(
            self,
            session: Session,
            user_id: str,
            message: str,
            notification_type: str,
            project_id: str = None,
    ) -> Notification:
        """
        Creates a notification for a specific user, saves it to the database,
        and broadcasts it via WebSockets.
        """
        notification = Notification(
            user_id=user_id,
            project_id=project_id,
            message=self._serialize_message(message),
            type=notification_type,
        )
        session.add(notification)
        session.commit()
        session.refresh(notification)

        # Broadcast the notification
        try:
            notif_data = NotificationPublic.model_validate(notification).model_dump()
            run_coro_safely(
                broadcaster.broadcast(f"notifications:{user_id}", notif_data)
            )
        except Exception as e:
            logger.error(f"Failed to broadcast notification to user {user_id}: {e}")

        return notification

    def notify_project_members(
        self,
        session: Session,
        project_id: str,
        message: str,
        notification_type: str,
        exclude_user_id: str = None,
    ):
        """
        Sends a notification to all users who are members of a specific project.
        """
        # Get all users in the project
        user_projects = (
            session.query(UserProject)
            .filter(UserProject.project_id == project_id)
            .all()
        )

        for up in user_projects:
            if exclude_user_id and up.user_id == exclude_user_id:
                continue
            
            # We reuse notify_user for each user
            # Note: notify_user does a commit/refresh each time. 
            # For many users, this might be slow, but for now it's centered logic.
            self.notify_user(
                session=session,
                user_id=up.user_id,
                message=self._serialize_message(message),
                notification_type=notification_type,
                project_id=project_id
            )

    def mark_all_as_read(self, session: Session, user_id: str):
        """
        Marks all unread notifications for a specific user as read.
        """
        session.query(Notification).filter(
            Notification.user_id == user_id, 
            Notification.is_read == False
        ).update({"is_read": True}, synchronize_session=False)
        session.commit()

    def purge_old_notifications(self, session: Session, days: int = 15) -> int:
        """
        Deletes notifications older than the specified number of days.
        Returns the number of deleted records.
        """
        threshold_date = datetime.now() - timedelta(days=days)
        
        deleted_count = (
            session.query(Notification)
            .filter(Notification.created_at < threshold_date)
            .delete(synchronize_session=False)
        )
        session.commit()
        logger.info(f"Purged {deleted_count} notifications older than {days} days.")
        return deleted_count

notification_service = NotificationService()