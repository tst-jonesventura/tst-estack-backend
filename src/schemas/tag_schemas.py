from pydantic import ConfigDict, Field

from schemas.base import Base


class TagBase(Base):
    tag: str = Field(
        ..., description="The name of the tag - must be short and descriptive"
    )


class TagPublic(TagBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    project_id: str
