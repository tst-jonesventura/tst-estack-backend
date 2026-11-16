from datetime import datetime

from pydantic import Field

from schemas.base import Base


class TutorMessagePublic(Base):
    id: str
    role: str
    content: str
    status: str
    created_at: datetime


class TutorConversationPublic(Base):
    id: str
    project_id: str
    summary: str | None = None
    messages: list[TutorMessagePublic] = Field(default_factory=list)


class TutorMessageInput(Base):
    message: str = Field(min_length=1)
