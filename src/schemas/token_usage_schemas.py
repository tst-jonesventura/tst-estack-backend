from typing import List

from pydantic import EmailStr

from schemas.base import Base


class ModelTokenUsagePublic(Base):
    model: str
    agent_id: str | None = None
    agent_name: str | None = None
    total_tokens: int


class RankedTokenUsageItemPublic(Base):
    id: str
    project_id: str
    label: str
    total_tokens: int
    models: List[ModelTokenUsagePublic] = []


class ProjectTokenUsageDetailPublic(Base):
    id: str
    name: str
    contributors: List[RankedTokenUsageItemPublic] = []
    specifications: List[RankedTokenUsageItemPublic] = []
    agents: List[RankedTokenUsageItemPublic] = []


class UserTokenUsageDetailPublic(Base):
    id: str
    email: EmailStr
    projects: List[RankedTokenUsageItemPublic] = []
    specifications: List[RankedTokenUsageItemPublic] = []
    agents: List[RankedTokenUsageItemPublic] = []
