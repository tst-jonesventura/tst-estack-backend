import datetime
from typing import List, Optional

from pydantic import Field

from schemas import tag_schemas
from schemas.base import Base, date_now


class DocumentTranscriptionBase(Base):
    title: str = Field(..., description="The title of the document")
    content: str = Field(..., description="The textual content of the document")
    created_at: datetime.datetime = Field(default_factory=date_now)


class DocumentTranscriptionCreate(DocumentTranscriptionBase):
    pass


class DocumentTranscriptionPublic(DocumentTranscriptionBase):
    id: str
    project_id: str | None = None
    tags: List[tag_schemas.TagBase] = []


class DocumentTranscriptionUpdate(Base):
    title: Optional[str] = None
    content: Optional[str] = None


class CreateTranscriptionResponse(Base):
    transcription_id: str
    execution_id: str
