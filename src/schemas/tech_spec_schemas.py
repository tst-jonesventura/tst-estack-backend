import datetime
from typing import List, Optional

from pydantic import Field

from schemas import tag_schemas
from schemas.base import Base, date_now


class DocumentTechSpecBase(Base):
    """Model for technical specification documents. Each tech spec should be granular and focused on a specific aspect of the project."""

    title: str = Field(..., description="The title of the document")
    content: str = Field(..., description="The textual content of the document")
    created_at: datetime.datetime = Field(default_factory=date_now)


class DocumentTechSpecBaseWithId(DocumentTechSpecBase):
    id: str = Field(
        ..., description="The unique uuid4 of the technical specification document"
    )


class DocumentTechSpecCreate(DocumentTechSpecBase):
    pass


class DocumentTechSpecPublic(DocumentTechSpecBase):
    id: str
    project_id: str | None = None
    tags: List[tag_schemas.TagBase] = []


class DocumentTechSpecVersionPublic(DocumentTechSpecBase):
    id: str
    transaction_id: int
    operation_type: int
    log: str
    user_id: str
    transcription_id: str | None = None


class DocumentTechSpecUpdate(Base):
    title: Optional[str] = None
    content: Optional[str] = None
