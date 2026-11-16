import uuid
from datetime import datetime
from enum import Enum
from typing import List, Optional  # noqa: UP035

from schemas.base import date_now
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Unicode,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    configure_mappers,
    mapped_column,
    relationship,
)
from sqlalchemy_continuum import make_versioned, version_class
from utils.crypto import EncryptedString

# Constants for repeated table/field literals
TAG_TABLE = "tag"
TAG_ID = "tag.id"
TAG_ID_FIELD = "tag_id"
USER_STORY_TAG_TABLE = "user_story_tag"
TECH_SPEC_TAG_TABLE = "tech_spec_tag"
TRANSCRIPTION_TAG_TABLE = "transcription_tag"

USER_STORY_ID = "user_story.id"
DOCUMENT_TRANSCRIPTION_ID = "document_transcription.id"
USER_ID = "user.id"
AGENT_ID_FK = "agent.id"
AGENT_RUN_ID_FK = "agent_run.id"
PROJECT_ID = "project.id"
ORGANIZATION_ID_FK = "organization.id"
PROJECT_KANBAN_COLUMN_ID = "project_kanban_column.id"

DELETE_ALL = "all, delete-orphan"

make_versioned(user_cls=None)  # type: ignore


class Base(DeclarativeBase):
    pass


class ProjectKanbanColumnRuleActionType(str, Enum):
    TRIGGER = "TRIGGER"
    VALIDATION = "VALIDATION"


class ProjectKanbanColumnRuleProvider(str, Enum):
    INTERNAL = "INTERNAL"
    EXTERNAL = "EXTERNAL"


class McpProviderType(str, Enum):
    GITHUB = "github"
    LOCAL_FILESYSTEM = "local_filesystem"


class McpDomain(str, Enum):
    """Knowledge domain an MCP integration answers questions about."""

    SCM = "scm"
    BACKLOG = "backlog"
    WIKI = "wiki"
    OBSERVABILITY = "observability"
    CICD = "cicd"
    API_CATALOG = "api_catalog"
    DATABASE = "database"


class DatabaseEngine(str, Enum):
    POSTGRES = "postgres"
    DB2I = "db2i"
    ORACLE = "oracle"
    SQLSERVER = "sqlserver"
    DB2 = "db2"


class DatabaseAccessMode(str, Enum):
    DIRECT = "direct"
    CONNECTOR = "connector"


class ConnectorJobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    EXPIRED = "expired"


# region User Story and related models
class UserStory(Base):
    __tablename__ = "user_story"
    __versioned__ = {"exclude": ["tags", "sort_order"]}
    __table_args__ = (
        Index("ix_user_story_project_id_timestamp", "project_id", "timestamp"),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False)
    points: Mapped[int] = mapped_column(Integer, nullable=False)
    is_done: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    log: Mapped[str] = mapped_column(String)
    sort_order: Mapped[str] = mapped_column(String(255), unique=False, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=date_now, nullable=False
    )

    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(PROJECT_ID, ondelete="CASCADE", name="fk_user_story_project_id"),
    )
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(USER_ID, ondelete="CASCADE", name="fk_user_story_user_id"),
    )
    transcription_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(DOCUMENT_TRANSCRIPTION_ID, name="fk_user_story_transcription_id"),
        nullable=True,
    )
    project_kanban_column_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            PROJECT_KANBAN_COLUMN_ID, name="fk_user_story_project_kanban_column_id"
        ),
        nullable=True,
    )

    project: Mapped["Project"] = relationship("Project", back_populates="user_stories")
    gherkin_rules: Mapped[List["GherkinRule"]] = relationship(
        "GherkinRule",
        back_populates="user_story",
        cascade=DELETE_ALL,
        order_by="GherkinRule.index",
    )
    dev_steps: Mapped[List["UserStoryDevStep"]] = relationship(
        "UserStoryDevStep",
        back_populates="user_story",
        order_by="UserStoryDevStep.order",
        cascade=DELETE_ALL,
    )
    user: Mapped["User"] = relationship("User", back_populates="user_stories")
    transcription: Mapped["DocumentTranscription"] = relationship(
        "DocumentTranscription", back_populates="user_stories"
    )
    tags: Mapped[List["Tag"]] = relationship(
        "Tag",
        secondary="user_story_tag",
        back_populates="user_stories",
        cascade="all, delete",
    )
    kanban_column: Mapped["ProjectKanbanColumn"] = relationship(
        "ProjectKanbanColumn", back_populates="user_stories"
    )
    agent_runs: Mapped[List["AgentRun"]] = relationship(
        "AgentRun", back_populates="user_story"
    )
    branches: Mapped[List["UserStoryBranch"]] = relationship(
        "UserStoryBranch", back_populates="user_story", cascade=DELETE_ALL
    )


class GherkinRule(Base):
    __tablename__ = "gherkin_rule"
    __table_args__ = (Index("ix_gherkin_rule_user_story_id", "user_story_id"),)

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    background: Mapped[str] = mapped_column(String)
    user_story_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            USER_STORY_ID,
            ondelete="CASCADE",
            name="fk_gherkin_rule_user_story_id",
        ),
    )

    user_story: Mapped["UserStory"] = relationship(
        "UserStory", back_populates="gherkin_rules"
    )
    scenarios: Mapped[List["GherkinScenario"]] = relationship(
        "GherkinScenario",
        back_populates="gherkin_rule",
        cascade=DELETE_ALL,
        order_by="GherkinScenario.index",
    )


class GherkinScenario(Base):
    __tablename__ = "gherkin_scenario"
    __table_args__ = (Index("ix_gherkin_scenario_gherkin_rule_id", "gherkin_rule_id"),)

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    text: Mapped[str] = mapped_column(String, nullable=False)
    is_done: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    gherkin_rule_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "gherkin_rule.id",
            ondelete="CASCADE",
            name="fk_gherkin_scenario_gherkin_rule_id",
        ),
    )

    gherkin_rule: Mapped["GherkinRule"] = relationship(
        "GherkinRule", back_populates="scenarios"
    )


class UserStoryDevStep(Base):
    __tablename__ = "user_story_dev_step"
    __versioned__ = {}
    __table_args__ = (Index("ix_user_story_dev_step_user_story_id", "user_story_id"),)

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    order: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False)
    user_story_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            USER_STORY_ID,
            ondelete="CASCADE",
            name="fk_user_story_dev_step_user_story_id",
        ),
    )

    user_story: Mapped["UserStory"] = relationship(
        "UserStory", back_populates="dev_steps"
    )


class UserStoryBranch(Base):
    __tablename__ = "user_story_branch"
    __table_args__ = (
        Index("ix_user_story_branch_user_story_id", "user_story_id"),
        Index("ix_user_story_branch_project_repository_id", "project_repository_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    branch_name: Mapped[str] = mapped_column(String, nullable=False)
    branch_url: Mapped[str] = mapped_column(String, nullable=True)
    pull_request_url: Mapped[str] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=date_now, nullable=False
    )
    user_story_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            USER_STORY_ID,
            ondelete="CASCADE",
            name="fk_user_story_branch_user_story_id",
        ),
    )
    project_repository_id = Column(
        String(36),
        ForeignKey("project_repository.id", ondelete="NO ACTION"),
        nullable=False,
    )

    user_story: Mapped["UserStory"] = relationship(
        "UserStory", back_populates="branches"
    )
    project_repository: Mapped["ProjectRepository"] = relationship(
        "ProjectRepository", back_populates="user_story_branches"
    )


# endregion


# region Project and related models
class Organization(Base):
    __tablename__ = "organization"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    mcp_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=date_now, nullable=True
    )

    projects: Mapped[List["Project"]] = relationship(
        "Project", back_populates="organization", cascade=DELETE_ALL
    )
    github_mcp_config: Mapped["OrganizationGithubMcpConfig | None"] = relationship(
        "OrganizationGithubMcpConfig",
        back_populates="organization",
        cascade=DELETE_ALL,
        uselist=False,
    )
    mcp_integrations: Mapped[List["OrganizationMcpIntegration"]] = relationship(
        "OrganizationMcpIntegration",
        back_populates="organization",
        cascade=DELETE_ALL,
    )


class OrganizationMcpIntegration(Base):
    """Which MCP provider serves a given knowledge domain for a company.

    Routing only: credentials live in the provider's own configuration table.
    """

    __tablename__ = "organization_mcp_integration"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "domain",
            name="uq_organization_mcp_integration_organization_id_domain",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    organization_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            ORGANIZATION_ID_FK,
            ondelete="CASCADE",
            name="fk_organization_mcp_integration_organization_id",
        ),
        nullable=False,
    )
    domain: Mapped[str] = mapped_column(String(50), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=date_now, nullable=True
    )

    organization: Mapped["Organization"] = relationship(
        "Organization", back_populates="mcp_integrations"
    )


class OrganizationGithubMcpConfig(Base):
    """Per-organization GitHub MCP system configuration.

    Sensitive credentials (GitHub App id and private key) are encrypted at
    rest via :class:`~utils.crypto.EncryptedString`. Non-sensitive metadata
    such as the MCP endpoint URL is stored in plaintext for readability.
    """

    __tablename__ = "organization_github_mcp_config"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    organization_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            ORGANIZATION_ID_FK,
            ondelete="CASCADE",
            name="fk_organization_github_mcp_config_organization_id",
        ),
        unique=True,
        nullable=False,
    )
    github_app_id: Mapped[str] = mapped_column(EncryptedString, nullable=False)
    github_app_private_key_base64: Mapped[str] = mapped_column(
        EncryptedString, nullable=False
    )
    mcp_url: Mapped[str] = mapped_column(
        String, nullable=False, default="https://api.githubcopilot.com/mcp/"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=date_now, nullable=True
    )

    organization: Mapped["Organization"] = relationship(
        "Organization", back_populates="github_mcp_config"
    )


class Project(Base):
    __tablename__ = "project"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=date_now, nullable=True
    )

    organization_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            ORGANIZATION_ID_FK,
            ondelete="CASCADE",
            name="fk_project_organization_id",
        ),
        nullable=False,
    )

    organization: Mapped["Organization"] = relationship(
        "Organization", back_populates="projects"
    )
    user_stories: Mapped[List["UserStory"]] = relationship(
        "UserStory", back_populates="project", cascade=DELETE_ALL
    )
    document_transcriptions: Mapped[List["DocumentTranscription"]] = relationship(
        "DocumentTranscription", back_populates="project", cascade=DELETE_ALL
    )
    document_tech_specs: Mapped[List["DocumentTechSpec"]] = relationship(
        "DocumentTechSpec", back_populates="project", cascade=DELETE_ALL
    )
    user_roles: Mapped[List["UserProject"]] = relationship(
        "UserProject", back_populates="project", cascade=DELETE_ALL
    )
    tags: Mapped[List["Tag"]] = relationship("Tag", back_populates="project")
    repositories: Mapped[List["ProjectRepository"]] = relationship(
        "ProjectRepository", back_populates="project", cascade=DELETE_ALL
    )
    databases: Mapped[list["ProjectDatabase"]] = relationship(
        "ProjectDatabase", back_populates="project", cascade=DELETE_ALL
    )
    agent_overrides: Mapped[List["ProjectAgent"]] = relationship(
        "ProjectAgent", back_populates="project", cascade=DELETE_ALL
    )
    notifications: Mapped[List["Notification"]] = relationship(
        "Notification", back_populates="project", cascade=DELETE_ALL
    )


class ProjectDatabase(Base):
    """A database a project's agents may read, with its credentials encrypted at rest."""

    __tablename__ = "project_database"
    __table_args__ = (
        UniqueConstraint("project_id", "alias", name="uq_project_database_project_id_alias"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(PROJECT_ID, ondelete="CASCADE", name="fk_project_database_project_id"),
        nullable=False,
        index=True,
    )
    alias: Mapped[str] = mapped_column(Unicode(100), nullable=False)
    engine: Mapped[str] = mapped_column(
        String(20), nullable=False, default=DatabaseEngine.POSTGRES.value
    )
    access_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default=DatabaseAccessMode.DIRECT.value
    )
    connector_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("database_connector.id", name="fk_project_database_connector_id"),
        nullable=True,
        index=True,
    )
    connector_alias: Mapped[str | None] = mapped_column(Unicode(100), nullable=True)
    host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True, default=5432)
    database_name: Mapped[str] = mapped_column(Unicode(255), nullable=False)
    username: Mapped[str | None] = mapped_column(Unicode(255), nullable=True)
    password: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    ssl_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="require")
    schema_allowlist: Mapped[str | None] = mapped_column(Unicode(1000), nullable=True)
    description: Mapped[str | None] = mapped_column(Unicode(1000), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=date_now, nullable=True
    )

    project: Mapped["Project"] = relationship("Project", back_populates="databases")
    connector: Mapped["DatabaseConnector | None"] = relationship("DatabaseConnector")

    @property
    def schemas(self) -> tuple[str, ...]:
        if not self.schema_allowlist:
            return ()
        return tuple(name.strip() for name in self.schema_allowlist.split(",") if name.strip())


class DatabaseConnector(Base):
    """A connector running on a company's network that reads its databases for us.

    The database credentials stay with the connector; e-Stack only keeps a hash
    of the token the connector authenticates with.
    """

    __tablename__ = "database_connector"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    organization_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            ORGANIZATION_ID_FK, ondelete="CASCADE", name="fk_database_connector_organization_id"
        ),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(Unicode(100), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    token_hint: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reported_databases: Mapped[str | None] = mapped_column(Unicode, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=date_now, nullable=True
    )

    organization: Mapped["Organization"] = relationship("Organization")


class DatabaseConnectorJob(Base):
    """One operation waiting for, or answered by, a connector."""

    __tablename__ = "database_connector_job"
    __table_args__ = (
        Index("ix_database_connector_job_connector_status", "connector_id", "status"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    connector_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "database_connector.id", ondelete="CASCADE", name="fk_database_connector_job_connector_id"
        ),
        nullable=False,
    )
    database_alias: Mapped[str] = mapped_column(Unicode(100), nullable=False)
    operation: Mapped[str] = mapped_column(String(50), nullable=False)
    arguments: Mapped[str] = mapped_column(Unicode, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ConnectorJobStatus.PENDING.value
    )
    result: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    error: Mapped[str | None] = mapped_column(Unicode(2000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=date_now, nullable=False
    )
    picked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProjectRepository(Base):
    __tablename__ = "project_repository"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    source_type: Mapped[str] = mapped_column(
        String(50), default="github", nullable=False
    )
    local_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    owner: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    language: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    full_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    default_branch: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    project_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey(
            PROJECT_ID,
            ondelete="CASCADE",
            name="fk_project_repository_project_id",
        ),
        nullable=True,
    )
    organization_github_installation_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey(
            "organization_github_installation.id",
            ondelete="CASCADE",
            name="fk_project_repository_organization_github_installation_id",
        ),
        nullable=True,
    )

    project: Mapped["Project"] = relationship("Project")
    user_story_branches: Mapped[List["UserStoryBranch"]] = relationship(
        "UserStoryBranch", back_populates="project_repository", cascade=DELETE_ALL
    )


# endregion


# region Tag and related models
class Tag(Base):
    __tablename__ = TAG_TABLE

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            PROJECT_ID,
            ondelete="CASCADE",
            name="fk_project_tag_project_id",
        ),
    )
    tag: Mapped[str] = mapped_column(String, nullable=False)

    project: Mapped["Project"] = relationship("Project", back_populates="tags")
    user_stories: Mapped[List["UserStory"]] = relationship(
        "UserStory", secondary=USER_STORY_TAG_TABLE, back_populates="tags"
    )
    tech_specs: Mapped[List["DocumentTechSpec"]] = relationship(
        "DocumentTechSpec", secondary=TECH_SPEC_TAG_TABLE, back_populates="tags"
    )
    transcriptions: Mapped[List["DocumentTranscription"]] = relationship(
        "DocumentTranscription",
        secondary=TRANSCRIPTION_TAG_TABLE,
        back_populates="tags",
    )


class UserStoryTag(Base):
    __tablename__ = USER_STORY_TAG_TABLE

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_story_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            USER_STORY_ID,
            name="fk_user_story_tag_user_story_id",
        ),
    )
    tag_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            TAG_ID,
            ondelete="CASCADE",
            name="fk_user_story_tag_tag_id",
        ),
    )


class DocumentTranscription(Base):
    __tablename__ = "document_transcription"
    __table_args__ = (Index("ix_document_transcription_project_id", "project_id"),)

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=date_now, nullable=False
    )
    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            PROJECT_ID,
            ondelete="CASCADE",
            name="fk_document_transcription_project_id",
        ),
    )

    project: Mapped["Project"] = relationship(
        "Project", back_populates="document_transcriptions"
    )
    user_stories: Mapped[List["UserStory"]] = relationship(
        "UserStory", back_populates="transcription"
    )
    document_tech_specs: Mapped[List["DocumentTechSpec"]] = relationship(
        "DocumentTechSpec", back_populates="transcription"
    )
    tags: Mapped[List["Tag"]] = relationship(
        "Tag", secondary="transcription_tag", back_populates="transcriptions"
    )


class TranscriptionTag(Base):
    __tablename__ = "transcription_tag"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    transcription_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            DOCUMENT_TRANSCRIPTION_ID,
            name="fk_transcription_tag_document_transcription_id",
        ),
    )
    tag_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            TAG_ID,
            ondelete="CASCADE",
            name="fk_transcription_tag_tag_id",
        ),
    )


# endregion


# region Documents
class DocumentTechSpec(Base):
    __tablename__ = "document_tech_spec"
    __versioned__ = {"exclude": ["tags"]}
    __table_args__ = (Index("ix_document_tech_spec_project_id", "project_id"),)

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=date_now, nullable=False
    )
    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            PROJECT_ID, ondelete="CASCADE", name="fk_document_tech_spec_project_id"
        ),
    )

    log: Mapped[str] = mapped_column(String)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey(USER_ID, name="fk_document_tech_spec_user_id")
    )

    transcription_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            DOCUMENT_TRANSCRIPTION_ID, name="fk_document_tech_spec_transcription_id"
        ),
        nullable=True,
    )

    project: Mapped["Project"] = relationship(
        "Project", back_populates="document_tech_specs"
    )
    user: Mapped["User"] = relationship("User", back_populates="document_tech_specs")
    transcription: Mapped["DocumentTranscription"] = relationship(
        "DocumentTranscription", back_populates="document_tech_specs"
    )
    tags: Mapped[List["Tag"]] = relationship(
        "Tag", secondary="tech_spec_tag", back_populates="tech_specs"
    )


class TechSpecTag(Base):
    __tablename__ = "tech_spec_tag"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    tech_spec_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "document_tech_spec.id",
            name="fk_tech_spec_tag_document_tech_spec_id",
        ),
    )
    tag_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            TAG_ID,
            ondelete="CASCADE",
            name="fk_tech_spec_tag_tag_id",
        ),
    )


# endregion


# region Agents and related models
class Agent(Base):
    __tablename__ = "agent"
    __table_args__ = (Index("ix_agent_name_is_default", "name", "is_default"),)

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=date_now, nullable=False
    )
    user_prompt: Mapped[str | None] = mapped_column(String, default=None, nullable=True)
    system_prompt: Mapped[str | None] = mapped_column(
        String, default=None, nullable=True
    )
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_retries: Mapped[int] = mapped_column(Integer)
    tools: Mapped[List["AgentTool"]] = relationship("AgentTool", back_populates="agent")
    context: Mapped[List["AgentContext"]] = relationship(
        "AgentContext", back_populates="agent"
    )
    mcps: Mapped[List["AgentMcp"]] = relationship("AgentMcp", back_populates="agent")
    project_overrides: Mapped[List["ProjectAgent"]] = relationship(
        "ProjectAgent", back_populates="agent", cascade=DELETE_ALL
    )
    agent_runs: Mapped[List["AgentRun"]] = relationship(
        "AgentRun", back_populates="agent"
    )


class ProjectAgent(Base):
    __tablename__ = "project_agent"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "agent_name",
            name="uq_project_agent_project_id_agent_name",
        ),
        Index("ix_project_agent_project_id_agent_name", "project_id", "agent_name"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(PROJECT_ID, ondelete="CASCADE", name="fk_project_agent_project_id"),
        nullable=False,
    )
    agent_name: Mapped[str] = mapped_column(String(100), nullable=False)
    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(AGENT_ID_FK, ondelete="CASCADE", name="fk_project_agent_agent_id"),
        nullable=False,
    )

    project: Mapped["Project"] = relationship(
        "Project", back_populates="agent_overrides"
    )
    agent: Mapped["Agent"] = relationship("Agent", back_populates="project_overrides")


class AgentRun(Base):
    __tablename__ = "agent_run"
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey(USER_ID, name="fk_agent_run_user_id")
    )
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey(PROJECT_ID, name="fk_agent_run_project_id")
    )
    agent_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey(AGENT_ID_FK, ondelete="SET NULL", name="fk_agent_run_agent_id"),
        nullable=True,
    )
    user_story_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey(USER_STORY_ID, name="fk_agent_run_user_story_id"),
        nullable=True,
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    input: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    is_internal: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    requests: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    input_audio_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    cache_audio_read_tokens: Mapped[int] = mapped_column(
        Integer, default=0, nullable=True
    )
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    queue_key: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=date_now, nullable=False
    )

    user: Mapped["User"] = relationship("User")
    agent: Mapped["Agent | None"] = relationship("Agent", back_populates="agent_runs")
    user_story: Mapped["UserStory"] = relationship(
        "UserStory", back_populates="agent_runs"
    )
    logs: Mapped[List["AgentLog"]] = relationship(
        "AgentLog",
        back_populates="agent_run",
        order_by="AgentLog.timestamp",
        cascade=DELETE_ALL,
    )


class AgentLog(Base):
    __tablename__ = "agent_log"
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    agent_run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_run.id", name="fk_agent_log_agent_run_id"),
        nullable=True,
    )
    message: Mapped[str] = mapped_column(String, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=date_now, nullable=False
    )
    agent_run: Mapped["AgentRun"] = relationship("AgentRun", back_populates="logs")


class AgentTool(Base):
    __tablename__ = "agent_tool"
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey(AGENT_ID_FK, name="fk_agent_tool_agent_id")
    )
    tool_name: Mapped[str] = mapped_column(
        String(100), ForeignKey("tool.name", name="fk_agent_tool_tool_name")
    )
    agent: Mapped["Agent"] = relationship("Agent", back_populates="tools")
    tool: Mapped["Tool"] = relationship("Tool", back_populates="agents")


class AgentContext(Base):
    __tablename__ = "agent_context"

    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(AGENT_ID_FK, name="fk_agent_context_agent_id"),
        primary_key=True,
    )
    context_name: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("context.name", name="fk_agent_context_context_name"),
        primary_key=True,
    )
    agent: Mapped["Agent"] = relationship("Agent", back_populates="context")
    context: Mapped["Context"] = relationship("Context", back_populates="agents")


class AgentMcp(Base):
    __tablename__ = "agent_mcp"

    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(AGENT_ID_FK, name="fk_agent_mcp_agent_id"),
        primary_key=True,
    )
    mcp_name: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("mcp.name", name="fk_agent_mcp_mcp_name"),
        primary_key=True,
    )
    agent: Mapped["Agent"] = relationship("Agent", back_populates="mcps")
    mcp: Mapped["Mcp"] = relationship("Mcp", back_populates="agents")


class Tool(Base):
    __tablename__ = "tool"

    name: Mapped[str] = mapped_column(String(100), nullable=False, primary_key=True)
    description: Mapped[str] = mapped_column(String)
    agents: Mapped[List["AgentTool"]] = relationship("AgentTool", back_populates="tool")


class Context(Base):
    __tablename__ = "context"

    name: Mapped[str] = mapped_column(String(100), nullable=False, primary_key=True)
    description: Mapped[str] = mapped_column(String)
    agents: Mapped[List["AgentContext"]] = relationship(
        "AgentContext", back_populates="context"
    )


class Mcp(Base):
    __tablename__ = "mcp"

    name: Mapped[str] = mapped_column(String(100), nullable=False, primary_key=True)
    description: Mapped[str] = mapped_column(String)
    agents: Mapped[List["AgentMcp"]] = relationship("AgentMcp", back_populates="mcp")


class TutorConversation(Base):
    __tablename__ = "tutor_conversation"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "user_id",
            name="uq_tutor_conversation_project_user",
        ),
        Index(
            "ix_tutor_conversation_project_user",
            "project_id",
            "user_id",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            PROJECT_ID, ondelete="CASCADE", name="fk_tutor_conversation_project_id"
        ),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(USER_ID, name="fk_tutor_conversation_user_id"),
        nullable=False,
    )
    summary: Mapped[str | None] = mapped_column(Unicode, default=None, nullable=True)
    summarized_message_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=date_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=date_now, onupdate=date_now, nullable=False
    )

    messages: Mapped[List["TutorMessage"]] = relationship(
        "TutorMessage",
        back_populates="conversation",
        order_by="TutorMessage.created_at",
        cascade=DELETE_ALL,
    )


class TutorMessage(Base):
    __tablename__ = "tutor_message"
    __table_args__ = (
        Index("ix_tutor_message_conversation_status", "conversation_id", "status"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    conversation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "tutor_conversation.id",
            ondelete="CASCADE",
            name="fk_tutor_message_conversation_id",
        ),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Unicode, nullable=False)
    # Lifecycle of an assistant turn: "processing" while the background task runs,
    # "completed" once the reply is ready, "failed" if the turn errored. User messages
    # are always stored as "completed".
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="completed", server_default="completed"
    )
    agent_run_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey(AGENT_RUN_ID_FK, name="fk_tutor_message_agent_run_id"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=date_now, nullable=False
    )

    conversation: Mapped["TutorConversation"] = relationship(
        "TutorConversation", back_populates="messages"
    )
    agent_run: Mapped["AgentRun | None"] = relationship("AgentRun")


class OpenHandsConversation(Base):
    """An OpenHands session the user chats with before executing a task.

    The SDK persists its own event history under ``workspace_dir`` keyed by
    ``openhands_conversation_id``; that directory is a cache and may be wiped, so the
    messages in ``openhands_message`` are the durable transcript.
    """

    __tablename__ = "openhands_conversation"
    __table_args__ = (
        Index(
            "ix_openhands_conversation_project_user",
            "project_id",
            "user_id",
        ),
        Index(
            "ix_openhands_conversation_project_user_status",
            "project_id",
            "user_id",
            "status",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            PROJECT_ID, ondelete="CASCADE", name="fk_openhands_conversation_project_id"
        ),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(USER_ID, name="fk_openhands_conversation_user_id"),
        nullable=False,
    )
    openhands_conversation_id: Mapped[str] = mapped_column(
        String(36), nullable=False, default=lambda: str(uuid.uuid4())
    )
    workspace_dir: Mapped[str] = mapped_column(String, nullable=False)
    # Goal preset id (see ``agents.openhands_goals``).
    goal: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active"
    )
    branch_name: Mapped[str | None] = mapped_column(String, nullable=True)
    pull_request_url: Mapped[str | None] = mapped_column(String, nullable=True)
    pull_request_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=date_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=date_now, onupdate=date_now, nullable=False
    )

    project: Mapped["Project"] = relationship("Project")
    messages: Mapped[List["OpenHandsMessage"]] = relationship(
        "OpenHandsMessage",
        back_populates="conversation",
        order_by="OpenHandsMessage.created_at",
        cascade=DELETE_ALL,
    )


class OpenHandsMessage(Base):
    __tablename__ = "openhands_message"
    __table_args__ = (
        Index("ix_openhands_message_conversation_status", "conversation_id", "status"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    conversation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "openhands_conversation.id",
            ondelete="CASCADE",
            name="fk_openhands_message_conversation_id",
        ),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(String, nullable=False)
    # JSON list of completed transient thoughts so SSE works on any worker. Cleared
    # once the turn finishes.
    stream_content: Mapped[str | None] = mapped_column(String, nullable=True)
    # Same lifecycle as TutorMessage: "processing" while the background task runs,
    # "completed" once the reply is ready, "failed" if the turn errored.
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="completed", server_default="completed"
    )
    agent_run_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey(AGENT_RUN_ID_FK, name="fk_openhands_message_agent_run_id"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=date_now, nullable=False
    )

    conversation: Mapped["OpenHandsConversation"] = relationship(
        "OpenHandsConversation", back_populates="messages"
    )
    agent_run: Mapped["AgentRun | None"] = relationship("AgentRun")


# endregion


# region User and related models
class Notification(Base):
    __tablename__ = "notification"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey(USER_ID), index=True, nullable=True
    )
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey(PROJECT_ID), index=True, nullable=True
    )
    message: Mapped[str] = mapped_column(String)
    type: Mapped[str] = mapped_column(String)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    toast_shown: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=date_now, nullable=False
    )

    user: Mapped["User"] = relationship("User", back_populates="notifications")
    project: Mapped["Project"] = relationship("Project", back_populates="notifications")


class User(Base):
    __tablename__ = "user"
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    email: Mapped[str] = mapped_column(
        String(254), unique=True, index=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=date_now, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    access_codes: Mapped[List["UserAccessCode"]] = relationship(
        "UserAccessCode", back_populates="user"
    )
    notifications: Mapped[List["Notification"]] = relationship(
        "Notification", back_populates="user", cascade=DELETE_ALL
    )
    project_roles: Mapped[List["UserProject"]] = relationship(
        "UserProject", back_populates="user"
    )
    user_stories: Mapped[List["UserStory"]] = relationship(
        "UserStory", back_populates="user"
    )
    document_tech_specs: Mapped[List["DocumentTechSpec"]] = relationship(
        "DocumentTechSpec", back_populates="user"
    )


class UserAccessCode(Base):
    __tablename__ = "user_access_code"
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_email: Mapped[str] = mapped_column(
        String(254),
        ForeignKey(
            "user.email", ondelete="CASCADE", name="fk_user_access_code_user_email"
        ),
    )
    access_code: Mapped[str] = mapped_column(String(4), nullable=False)
    access_code_expiry: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    user: Mapped["User"] = relationship("User", back_populates="access_codes")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=date_now, nullable=False
    )


class Role(Base):
    __tablename__ = "roles"
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(
        String(50), unique=True, nullable=False
    )  # e.g., 'admin', 'contributor', 'viewer'
    description: Mapped[str] = mapped_column(String(255))


class UserProject(Base):
    __tablename__ = "user_project_role"
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(USER_ID, ondelete="CASCADE", name="fk_user_project_role_user_id"),
    )
    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            PROJECT_ID, ondelete="CASCADE", name="fk_user_project_role_project_id"
        ),
    )
    role_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("roles.id", name="fk_user_project_role_role_id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=date_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=date_now, onupdate=date_now, nullable=False
    )
    user: Mapped["User"] = relationship("User")
    project: Mapped["Project"] = relationship("Project")
    role: Mapped["Role"] = relationship("Role")


class ProjectKanbanColumn(Base):
    __tablename__ = "project_kanban_column"
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    column_change_warning: Mapped[str | None] = mapped_column(String, nullable=True)

    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            PROJECT_ID,
            ondelete="CASCADE",
            name="fk_project_kanban_column_project_id",
        ),
    )

    user_stories: Mapped[List["UserStory"]] = relationship(
        "UserStory", back_populates="kanban_column"
    )
    rules: Mapped[List["ProjectKanbanColumnRule"]] = relationship(
        "ProjectKanbanColumnRule",
        back_populates="kanban_column",
        cascade=DELETE_ALL,
        order_by="ProjectKanbanColumnRule.order",
    )


class ProjectKanbanColumnRule(Base):
    __tablename__ = "project_kanban_column_rule"
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    action_type: Mapped[ProjectKanbanColumnRuleActionType] = mapped_column(
        String(50), nullable=False
    )
    provider: Mapped[ProjectKanbanColumnRuleProvider] = mapped_column(
        String(50), nullable=False
    )
    handler_key: Mapped[str] = mapped_column(String(100), nullable=False)
    error_message: Mapped[str | None] = mapped_column(String(255), nullable=True)
    order: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_schema: Mapped[str | None] = mapped_column(String, nullable=True)
    is_blocking: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    target_kanban_column_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            PROJECT_KANBAN_COLUMN_ID,
            ondelete="CASCADE",
            name="fk_project_kanban_column_rule_kanban_column_id",
        ),
    )
    kanban_column: Mapped["ProjectKanbanColumn"] = relationship(
        "ProjectKanbanColumn", back_populates="rules"
    )


class OrganizationGitHubAppInstallation(Base):
    __tablename__ = "organization_github_installation"
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    github_app_installation_id: Mapped[int] = mapped_column(Integer, nullable=False)
    github_account_login: Mapped[str] = mapped_column(String(255), nullable=True)
    github_org_name: Mapped[str] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=date_now, nullable=True
    )

    organization_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            ORGANIZATION_ID_FK,
            name="fk_project_github_installations_organization_id",
        ),
    )


# endregion


# region jira


class JiraIntegrationConfig(Base):
    __tablename__ = "jira_integration_config"
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("project.id", ondelete="CASCADE"), unique=True
    )
    jira_base_url: Mapped[str] = mapped_column(String, nullable=False)
    jira_email: Mapped[str] = mapped_column(String, nullable=False)
    jira_api_token: Mapped[str] = mapped_column(String, nullable=False)
    jira_project_id: Mapped[str] = mapped_column(String, nullable=False)
    jira_project_key: Mapped[str] = mapped_column(String, nullable=False)
    default_issue_type: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=date_now, nullable=True
    )

    project: Mapped["Project"] = relationship("Project")
    column_mappings: Mapped[List["JiraIntegrationColumnsMapping"]] = relationship(
        "JiraIntegrationColumnsMapping", back_populates="config", cascade=DELETE_ALL
    )
    card_mapping: Mapped[List["JiraIntegrationCardsMapping"]] = relationship(
        "JiraIntegrationCardsMapping", back_populates="config", cascade=DELETE_ALL
    )


class JiraIntegrationCardsMapping(Base):
    __tablename__ = "jira_integration_cards_mapping"
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_story_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("user_story.id", ondelete="CASCADE"), unique=True
    )
    jira_issue_key: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True
    )
    jira_integration_config_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("jira_integration_config.id"),
        nullable=False,
    )
    last_synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=date_now, nullable=True
    )

    user_story: Mapped["UserStory"] = relationship("UserStory")
    config: Mapped["JiraIntegrationConfig"] = relationship(
        "JiraIntegrationConfig", back_populates="card_mapping"
    )


class JiraIntegrationColumnsMapping(Base):
    __tablename__ = "jira_integration_columns_mapping"
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    project_kanban_column_id: Mapped[str] = mapped_column(
        String(36), ForeignKey(PROJECT_KANBAN_COLUMN_ID), nullable=False
    )
    jira_status_id: Mapped[str] = mapped_column(String(255), nullable=True)
    jira_status_name: Mapped[str] = mapped_column(String(255), nullable=False)
    jira_integration_config_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("jira_integration_config.id", ondelete="CASCADE"),
        nullable=False,
    )

    kanban_column: Mapped["ProjectKanbanColumn"] = relationship("ProjectKanbanColumn")
    config: Mapped["JiraIntegrationConfig"] = relationship(
        "JiraIntegrationConfig", back_populates="column_mappings"
    )


# endregion


configure_mappers()
UserStoryVersion = version_class(UserStory)
UserStoryDevStepVersion = version_class(UserStoryDevStep)
DocumentTechSpecVersion = version_class(DocumentTechSpec)
