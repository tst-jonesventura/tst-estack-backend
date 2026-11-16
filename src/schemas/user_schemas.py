from datetime import datetime
from typing import List

from pydantic import BaseModel, EmailStr

from schemas.base import Base


class UserBase(Base):
    email: EmailStr
    is_active: bool = True
    is_admin: bool = False


class UserCreate(UserBase):
    pass


class UserProjectCreate(Base):
    user_id: str
    project_id: str
    role_id: str


class UserPublic(UserBase):
    created_at: datetime
    id: str


class UserProjectPublic(Base):
    id: str
    user_id: str
    project_id: str
    role_id: str
    created_at: datetime
    updated_at: datetime


class RolePublic(Base):
    id: str
    name: str
    description: str | None = None


class UserProjectWithUserAndRolePublic(Base):
    id: str
    user_id: str
    project_id: str
    role_id: str
    created_at: datetime
    updated_at: datetime
    user: UserPublic
    role: RolePublic


class UserUpdate(Base):
    is_active: bool | None = None
    is_admin: bool | None = None


class UserProjectUpdate(Base):
    user_id: str | None = None
    project_id: str | None = None
    role_id: str | None = None


class UserEmail(Base):
    email: str


class UserCode(Base):
    email: str
    code: str


class Token(BaseModel):
    """Token response model for OAuth2."""

    access_token: str
    token_type: str = "bearer"


class UserModelTokenUsagePublic(Base):
    model: str
    agent_id: str | None = None
    agent_name: str | None = None
    total_tokens: int


class UserTokenUsagePublic(Base):
    id: str
    email: EmailStr
    models: List[UserModelTokenUsagePublic] = []
