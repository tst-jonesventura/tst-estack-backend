"""Schemas exclusive to the OpenHands agents (one-shot task and conversation)."""

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator
from schemas.base import Base

OpenHandsConversationStatus = Literal["active", "opening_pr", "pr_opened", "pr_failed"]


class OpenHandsRepository(Base):
    """The subset of a project repository the OpenHands agent needs."""

    full_name: str
    default_branch: str


class OpenHandsTaskRequest(Base):
    prompt: str = Field(..., description="Instructions for the OpenHands agent")


class OpenHandsTaskResult(Base):
    output_message: str


class OpenHandsMessagePublic(Base):
    id: str
    role: str
    content: str
    status: str
    # JSON list of completed thoughts, present only while the turn is processing.
    stream_content: str | None = None
    created_at: datetime


class OpenHandsConversationSummary(Base):
    id: str
    project_id: str
    status: OpenHandsConversationStatus
    goal: str
    name: str | None = None
    branch_name: str | None = None
    pull_request_url: str | None = None
    pull_request_number: int | None = None
    created_at: datetime
    updated_at: datetime


class OpenHandsConversationPublic(OpenHandsConversationSummary):
    messages: list[OpenHandsMessagePublic] = Field(default_factory=list)


class OpenHandsConversationUpdate(Base):
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, value):
        return value.strip() if isinstance(value, str) else value


class OpenHandsMessageInput(Base):
    message: str = Field(min_length=1, description="Message to the agent")
