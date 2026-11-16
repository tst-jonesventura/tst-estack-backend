import datetime
from typing import List, Literal, Optional
from pydantic import Field

from schemas.base import Base, date_now
from schemas.user_schemas import UserPublic

from schemas.user_story_schemas import UserStoryPublic


class AgentBase(Base):
    name: str
    model: str
    is_default: bool = False
    created_at: datetime.datetime = Field(default_factory=date_now)
    user_prompt: str | None = None
    system_prompt: str | None = None
    temperature: float | None = None
    max_retries: int


class AgentToolBase(Base):
    name: str
    description: str | None = None


class MCPBase(Base):
    name: str
    description: str | None = None


class AgentContextBase(Base):
    name: str
    description: str | None = None


class AgentRunBase(Base):
    title: str
    status: str
    input: Optional[str] = None
    user_story_id: Optional[str] = None
    agent_id: Optional[str] = None


class AgentLogBase(Base):
    message: str
    timestamp: datetime.datetime = Field(default_factory=date_now)


class ExecutionId(Base):
    execution_id: str


class AgentCreate(AgentBase):
    tool_names: List[str] = []
    context_names: List[str] = []
    mcp_names: List[str] = []


class ToolPublic(AgentToolBase):
    pass


class ContextPublic(AgentContextBase):
    pass


class MCPPublic(MCPBase):
    pass


class AgentPublic(AgentBase):
    id: str
    tools: List[ToolPublic] = Field(default_factory=list)
    contexts: List[ContextPublic] = Field(default_factory=list)
    mcps: List[MCPPublic] = Field(default_factory=list)


class AgentLogPublic(AgentLogBase):
    id: str


class AgentShortPublic(AgentBase):
    id: str
    name: str


class AgentRunPublic(AgentRunBase):
    id: str
    created_at: datetime.datetime
    user: UserPublic
    logs: List[AgentLogPublic] = Field(default_factory=list)
    user_story: Optional[UserStoryPublic] = None
    agent: Optional[AgentShortPublic] = None


class AgentRunPublicPage(Base):
    pages: int
    data: List[AgentRunPublic]


AgentRunPublic.model_rebuild()
AgentRunPublicPage.model_rebuild()


class ExecutionIdPublic(ExecutionId):
    pass


class AgentUpdate(Base):
    name: Optional[str] = None
    model: Optional[str] = None
    is_default: Optional[bool] = None
    user_prompt: Optional[str] = None
    system_prompt: Optional[str] = None
    temperature: Optional[float] = None
    max_retries: Optional[int] = None
    tool_names: Optional[List[str]] = None
    context_names: Optional[List[str]] = None
    mcp_names: Optional[List[str]] = None


class ProjectAgentEffectivePublic(Base):
    agent_name: str
    source: Literal["override", "default"]
    agent: AgentPublic
    default_agent_id: str | None = None
    override_agent_id: str | None = None


class ProjectAgentOverrideUpsert(Base):
    agent_id: str


class ProjectAgentOverrideFromDefaultCreate(Base):
    # Editable fields for the clone
    model: str | None = None
    user_prompt: str | None = None
    system_prompt: str | None = None
    temperature: float | None = None
    max_retries: int | None = None
    tool_names: List[str] | None = None
    context_names: List[str] | None = None
    mcp_names: List[str] | None = None
