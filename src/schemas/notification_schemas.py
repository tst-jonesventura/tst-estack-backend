import datetime
from schemas.base import Base

class NotificationBase(Base):
    project_id: str
    message: str
    type: str
    is_read: bool = False
    toast_shown: bool = False
    created_at: datetime.datetime

class NotificationPublic(NotificationBase):
    id: str
