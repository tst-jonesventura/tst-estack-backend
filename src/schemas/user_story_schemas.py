import datetime
from typing import List, Optional

from database.models import (
    ProjectKanbanColumnRuleActionType,
    ProjectKanbanColumnRuleProvider,
)
from pydantic import Field

from schemas import tag_schemas
from schemas.base import Base
from schemas.user_schemas import UserPublic


PROJECT_KANBAN_COLUMN_NAME_DESCRIPTION = "The name of the kanban column"


# Base models
class UserStoryBase(Base):
    """Model for user stories"""

    name: str = Field(..., description="The name of the user story")
    description: str = Field(
        ..., description="A detailed description of the user story"
    )
    points: int = Field(..., description="The story points assigned to the user story")
    is_done: bool = Field(False, description="Indicates if the user story is completed")
    timestamp: datetime.datetime = Field(
        ..., description="The timestamp when the user story was created or last updated"
    )
    project_id: str = Field(
        ..., description="The ID of the project this user story belongs to"
    )


class UserStoryBaseLLM(Base):
    """Model for user stories used in LLM interactions"""

    name: str = Field(..., description="The name of the user story")
    description: str = Field(
        ..., description="A detailed description of the user story"
    )
    points: int = Field(..., description="The story points assigned to the user story")
    is_done: bool = Field(False, description="Indicates if the user story is completed")


class UserStoryDevStepBase(Base):
    order: int = Field(
        ..., description="The order of execution of the development step"
    )
    name: str = Field(..., description="The name of the development step")
    description: str = Field(
        ...,
        description="""A detailed description of the development step. 
        It should include what needs to be done and any relevant details.
        Some details include:
        - What code components need to be created or modified
        - Any specific technologies or frameworks to be used
        - Any risks or considerations to keep in mind during implementation
        - Any missing information that needs to be gathered before starting the step""",
    )


class UserStoryUpdate(Base):
    name: Optional[str] = None
    sort_order: Optional[str] = None
    project_kanban_column_id: Optional[str] = None
    description: Optional[str] = None
    points: Optional[int] = None
    is_done: Optional[bool] = None


class UserStoryDevStepUpdate(Base):
    order: Optional[int] = None
    name: Optional[str] = None
    description: Optional[str] = None
    input_data: Optional[str] = None
    expected_output: Optional[str] = None


class GherkinRuleBase(Base):
    index: int = Field(..., description="The order index of the Gherkin rule")
    title: str = Field(..., description="The title of the Gherkin rule")
    background: str = Field(..., description="Background information for the rule")


class GherkinScenarioBase(Base):
    index: int = Field(..., description="The order index of the Gherkin scenario")
    title: str = Field(..., description="The title of the Gherkin scenario")
    text: str = Field(..., description="The scenario text")
    is_done: bool = Field(False, description="Indicates if the scenario is completed")


class ProjectKanbanColumnBase(Base):
    id: str = Field(..., description="The unique uuid4 of the kanban column")
    name: str = Field(..., description=PROJECT_KANBAN_COLUMN_NAME_DESCRIPTION)
    position: int = Field(..., description="The position of the kanban column")
    column_change_warning: Optional[str] = Field(None, description="Warning message if column cannot be changed")


class ProjectKanbanColumnRuleBase(Base):
    id: str
    order: int
    action_type: ProjectKanbanColumnRuleActionType
    provider: ProjectKanbanColumnRuleProvider
    handler_key: str
    is_blocking: bool
    target_kanban_column_id: str
    error_message: Optional[str] = None
    payload_schema: Optional[str] = None


class ProjectKanbanColumnRuleCreateUpdate(Base):
    order: int = Field(
        0,
        description="The order index of the rule among all rules for the target kanban column",
    )
    action_type: ProjectKanbanColumnRuleActionType = Field(
        ProjectKanbanColumnRuleActionType.TRIGGER,
        description="The type of action this rule checks for, e.g. 'VALIDATION' or 'TRIGGER'",
    )
    provider: ProjectKanbanColumnRuleProvider = Field(
        ProjectKanbanColumnRuleProvider.INTERNAL, description="The provider of the rule, e.g. 'INTERNAL' or 'EXTERNAL'"
    )
    handler_key: str = Field(
        ..., description="A unique key to identify the handler function for this rule"
    )
    is_blocking: bool = Field(
        False,
        description="Indicates if the rule should block the kanban column change if the rule check fails",
    )
    target_kanban_column_id: str = Field(
        None, description="The ID of the target kanban column for this rule"
    )
    error_message: Optional[str] = Field(
        "", description="An error message to display if the rule check fails"
    )
    payload_schema: Optional[str] = Field(
        "", description="The schema for the payload of the rule"
    )


# Child models
class UserStoryBaseLLMWithId(UserStoryBaseLLM):
    id: str = Field(..., description="The unique uuid4 of the user story")


class GherkinRulePublic(GherkinRuleBase):
    id: str


class GherkinScenarioPublic(GherkinScenarioBase):
    id: str
    gherkin_rule_id: str = Field(..., description="The ID of the related Gherkin rule")


class GherkinRuleFullPublic(GherkinRulePublic):
    scenarios: List[GherkinScenarioPublic] = []


class GherkinRuleFullToAgentRunPublic(GherkinRuleBase):
    scenarios: List[GherkinScenarioBase] = []


class UserStoryBaseComplete(UserStoryBase):
    dev_steps: List[UserStoryDevStepBase] = []
    gherkin_rules: List[GherkinRuleBase] = []
    tags: List[tag_schemas.TagBase] = []


class UserStoryPublic(UserStoryBase):
    id: str
    sort_order: str | None = None
    project_kanban_column_id: str | None = None


class UserStoryPublicWithTags(UserStoryPublic):
    """UserStoryPublic including tags, used where consumers need to reason about tags
    (e.g. LLM tool/context outputs) without pulling in dev_steps/gherkin/branches."""

    tags: List[tag_schemas.TagBase] = []


class UserStoryPublicWithGherkin(UserStoryPublic):
    gherkin_rules: List[GherkinRuleFullPublic] = []


class UserStoryDevStepPublic(UserStoryDevStepBase):
    id: str
    user_story_id: str | None = None


class UserStoryVersionPublic(UserStoryBase):
    tags: List[tag_schemas.TagBase] = []
    id: str
    transaction_id: int
    operation_type: int
    user: UserPublic
    log: str
    user_id: str
    transcription_id: Optional[str] = None


class UserStoryLastEventPublic(Base):
    description: str
    actor_email: Optional[str] = None
    occurred_at: datetime.datetime


class UserStoryBranchBase(Base):
    branch_name: str
    user_story_id: str
    branch_url: Optional[str]
    pull_request_url: Optional[str]
    project_repository_id: str


class UserStoryBranchCreate(UserStoryBranchBase):
    pass


class UserStoryBranchPublic(UserStoryBranchBase):
    id: str
    created_at: datetime.datetime


class UserStoryFullPublic(UserStoryPublic):
    dev_steps: List[UserStoryDevStepPublic] = []
    tags: List[tag_schemas.TagBase] = []
    gherkin_rules: List[GherkinRuleFullPublic] = []
    branches: List[UserStoryBranchPublic] = []
    kanban_column: ProjectKanbanColumnBase | None = None
    last_event: UserStoryLastEventPublic | None = None


class ProjectKanbanColumnWithUserStoriesPublic(ProjectKanbanColumnBase):
    user_stories: List[UserStoryPublic] = []


class UserStoryCreate(UserStoryBase):
    pass


class UserStoryDevStepCreate(UserStoryDevStepBase):
    pass


class GherkinRuleCreate(GherkinRuleBase):
    pass


class GherkinScenarioCreate(GherkinScenarioBase):
    pass


class ProjectKanbanColumnPublic(ProjectKanbanColumnBase):
    rules: List[ProjectKanbanColumnRuleBase] = []


class ProjectKanbanColumnCreate(Base):
    name: str = Field(..., description=PROJECT_KANBAN_COLUMN_NAME_DESCRIPTION)
    column_change_warning: Optional[str] = Field(
        None,
        description="An optional warning message shown when moving a user story into this column",
    )
    rules: List[ProjectKanbanColumnRuleCreateUpdate] = []


class ProjectKanbanColumnCreateResponse(ProjectKanbanColumnBase):
    column_change_warning: Optional[str] = None
    rules: List[ProjectKanbanColumnRuleBase] = []


class ProjectKanbanColumnUpdate(Base):
    name: Optional[str] = Field(
        None, description=PROJECT_KANBAN_COLUMN_NAME_DESCRIPTION
    )
    column_change_warning: Optional[str] = Field(
        None,
        description="An optional warning message shown when moving a user story into this column",
    )
    rules: List[ProjectKanbanColumnRuleCreateUpdate] = []


class ProjectKanbanColumnUpdateResponse(ProjectKanbanColumnBase):
    column_change_warning: Optional[str] = None
    rules: List[ProjectKanbanColumnRuleBase] = []


class ProjectKanbanColumnReorder(Base):
    id: str = Field(..., description="The unique uuid4 of the kanban column")
    position: int = Field(..., description="The new position of the kanban column")


class ProjectKanbanColumnRulePublic(ProjectKanbanColumnRuleBase):
    pass


class ProjectKanbanColumnFullPublic(ProjectKanbanColumnBase):
    column_change_warning: Optional[str] = None
    rules: List[ProjectKanbanColumnRulePublic] = []




class CodeGitAgentOutput(Base):
    branch_url: Optional[str] = Field(None, description="The URL of the created branch, if any")
    branch_name: Optional[str] = Field(None, description="The name of the created branch, if any")
    pull_request_url: Optional[str] = Field(None, description="The URL of the created pull request, if any")
    repository_url: Optional[str] = Field(None, description="The URL of the repository where the branch was created")
    output_message: str = Field(..., description="The message from the agent about the process")


class KanbanHandlersPublic(Base):
    handler_keys: List[str] = Field(
        ..., description="List of available handler keys registered in KanbanService"
    )
