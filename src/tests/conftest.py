import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "connector"))

os.environ.setdefault("ESTACK_DB_CONNECTION_STRING", "sqlite:///:memory:")
os.environ.setdefault("ESTACK_FRONT_HOST", "http://localhost:3000")
os.environ.setdefault("DEFAULT_MODEL", "gpt-4o")
os.environ.setdefault("LOGFIRE_TOKEN", "test-token")
os.environ.setdefault("SECRET_HASH_KEY", "test-secret-key")
os.environ.setdefault("EMAIL_USER", "test@example.com")
os.environ.setdefault("EMAIL_PASSWORD", "test-password")
os.environ.setdefault("EMAIL_HOST", "smtp.example.com")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:3000")
os.environ.setdefault("GITHUB_APP_ID", "0")
os.environ.setdefault("GITHUB_APP_PRIVATE_KEY_BASE64", "dGVzdA==")

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("ESTACK_CONFIG_ENCRYPTION_KEY", Fernet.generate_key().decode())

from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from database.database import get_session
from database.models import (
    Agent,
    AgentContext,
    AgentLog,
    AgentMcp,
    AgentRun,
    AgentTool,
    Base,
    Context,
    DocumentTechSpec,
    DocumentTranscription,
    GherkinRule,
    GherkinScenario,
    Mcp,
    Organization,
    OrganizationGitHubAppInstallation,
    Project,
    ProjectAgent,
    ProjectRepository,
    Role,
    Tag,
    TechSpecTag,
    Tool,
    TranscriptionTag,
    User,
    UserAccessCode,
    UserProject,
    UserStory,
    UserStoryDevStep,
    UserStoryTag,
)
from src.main import app
from utils.bearer_auth import (
    check_project_access,
    check_project_ownership,
    retrieve_admin_data,
    retrieve_user_data,
)


@pytest.fixture(autouse=True)
def _clear_process_caches():
    """Process-wide TTL caches outlive a test and would leak across the suite."""
    from database_access.guard import _posture_cache
    from database_access.operations import _catalog_cache
    from agents.utils.agent_factory import _agent_spec_cache
    from agents.utils.mcp_providers import (
        _accessible_repositories_cache,
        _installation_token_cache,
    )

    caches = (
        _agent_spec_cache,
        _installation_token_cache,
        _accessible_repositories_cache,
        _posture_cache,
        _catalog_cache,
    )
    for cache in caches:
        cache.clear()
    yield
    for cache in caches:
        cache.clear()


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine)
    session = TestSession()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@asynccontextmanager
async def _test_lifespan(_app):
    yield


@pytest.fixture
def client(db_session: Session):
    def _override_get_session():
        yield db_session

    original_router_lifespan = app.router.lifespan_context
    app.router.lifespan_context = _test_lifespan
    app.dependency_overrides[get_session] = _override_get_session
    with TestClient(app) as tc:
        yield tc
    app.dependency_overrides.clear()
    app.router.lifespan_context = original_router_lifespan


@pytest.fixture
def sample_user(db_session: Session) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email="testuser@example.com",
        is_active=True,
        is_admin=False,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def authenticated_user(client: TestClient, sample_user: User):
    app.dependency_overrides[retrieve_user_data] = lambda: sample_user
    app.dependency_overrides[retrieve_admin_data] = lambda: sample_user
    yield sample_user
    app.dependency_overrides.pop(retrieve_user_data, None)
    app.dependency_overrides.pop(retrieve_admin_data, None)


@pytest.fixture
def project_access_user(
    client: TestClient, authenticated_user: User
):
    app.dependency_overrides[check_project_access] = lambda: authenticated_user
    app.dependency_overrides[check_project_ownership] = lambda: authenticated_user
    yield authenticated_user
    app.dependency_overrides.pop(check_project_access, None)
    app.dependency_overrides.pop(check_project_ownership, None)


@pytest.fixture
def sample_role(db_session: Session) -> Role:
    role = Role(
        id=str(uuid.uuid4()),
        name="owner",
        description="Project owner",
    )
    db_session.add(role)
    db_session.commit()
    db_session.refresh(role)
    return role


@pytest.fixture
def sample_organization(db_session: Session) -> Organization:
    organization = Organization(
        id=str(uuid.uuid4()),
        name="Test Company",
        description="A test company",
        mcp_provider="github",
        is_active=True,
    )
    db_session.add(organization)
    db_session.commit()
    db_session.refresh(organization)
    return organization


@pytest.fixture
def sample_project(
    db_session: Session,
    sample_user: User,
    sample_role: Role,
    sample_organization: Organization,
) -> Project:
    project = Project(
        id=str(uuid.uuid4()),
        name="Test Project",
        description="A test project",
        is_active=True,
        organization_id=sample_organization.id,
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    user_project = UserProject(
        id=str(uuid.uuid4()),
        user_id=sample_user.id,
        project_id=project.id,
        role_id=sample_role.id,
    )
    db_session.add(user_project)
    db_session.commit()
    return project


@pytest.fixture
def sample_transcription(
    db_session: Session, sample_project: Project
) -> DocumentTranscription:
    transcription = DocumentTranscription(
        id=str(uuid.uuid4()),
        title="Test Transcription",
        content="This is a test transcription content.",
        project_id=sample_project.id,
    )
    db_session.add(transcription)
    db_session.commit()
    db_session.refresh(transcription)
    return transcription


@pytest.fixture
def sample_user_story(
    db_session: Session,
    sample_project: Project,
    sample_user: User,
    sample_transcription: DocumentTranscription,
) -> UserStory:
    story = UserStory(
        id=str(uuid.uuid4()),
        name="Test User Story",
        description="As a user I want to test fixtures",
        points=3,
        is_done=False,
        project_id=sample_project.id,
        user_id=sample_user.id,
        transcription_id=sample_transcription.id,
        log="",
    )
    db_session.add(story)
    db_session.commit()
    db_session.refresh(story)
    return story


@pytest.fixture
def sample_tech_spec(
    db_session: Session,
    sample_project: Project,
    sample_user: User,
    sample_transcription: DocumentTranscription,
) -> DocumentTechSpec:
    spec = DocumentTechSpec(
        id=str(uuid.uuid4()),
        title="Test Tech Spec",
        content="Technical specification content.",
        project_id=sample_project.id,
        user_id=sample_user.id,
        transcription_id=sample_transcription.id,
        log="",
    )
    db_session.add(spec)
    db_session.commit()
    db_session.refresh(spec)
    return spec


@pytest.fixture
def sample_gherkin_rule(
    db_session: Session, sample_user_story: UserStory
) -> GherkinRule:
    rule = GherkinRule(
        id=str(uuid.uuid4()),
        index=0,
        title="Test Gherkin Rule",
        background="Given a test environment",
        user_story_id=sample_user_story.id,
    )
    db_session.add(rule)
    db_session.commit()
    db_session.refresh(rule)
    return rule


@pytest.fixture
def sample_gherkin_scenario(
    db_session: Session, sample_gherkin_rule: GherkinRule
) -> GherkinScenario:
    scenario = GherkinScenario(
        id=str(uuid.uuid4()),
        index=0,
        title="Test Gherkin Scenario",
        text="When I run tests Then they should pass",
        is_done=False,
        gherkin_rule_id=sample_gherkin_rule.id,
    )
    db_session.add(scenario)
    db_session.commit()
    db_session.refresh(scenario)
    return scenario


@pytest.fixture
def sample_tag(db_session: Session, sample_project: Project) -> Tag:
    tag = Tag(
        id=str(uuid.uuid4()),
        project_id=sample_project.id,
        tag="test-tag",
    )
    db_session.add(tag)
    db_session.commit()
    db_session.refresh(tag)
    return tag


@pytest.fixture
def sample_admin_user(db_session: Session) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email="admin@example.com",
        is_active=True,
        is_admin=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def sample_inactive_user(db_session: Session) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email="inactive@example.com",
        is_active=False,
        is_admin=False,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def sample_contributor_role(db_session: Session) -> Role:
    role = Role(
        id=str(uuid.uuid4()),
        name="contributor",
        description="Project contributor",
    )
    db_session.add(role)
    db_session.commit()
    db_session.refresh(role)
    return role


@pytest.fixture
def sample_viewer_role(db_session: Session) -> Role:
    role = Role(
        id=str(uuid.uuid4()),
        name="viewer",
        description="Read-only project viewer",
    )
    db_session.add(role)
    db_session.commit()
    db_session.refresh(role)
    return role


@pytest.fixture
def sample_second_project(
    db_session: Session,
    sample_admin_user: User,
    sample_role: Role,
    sample_organization: Organization,
) -> Project:
    project = Project(
        id=str(uuid.uuid4()),
        name="Second Project",
        description="Another test project",
        is_active=True,
        organization_id=sample_organization.id,
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    user_project = UserProject(
        id=str(uuid.uuid4()),
        user_id=sample_admin_user.id,
        project_id=project.id,
        role_id=sample_role.id,
    )
    db_session.add(user_project)
    db_session.commit()
    return project


@pytest.fixture
def sample_second_transcription(
    db_session: Session, sample_project: Project
) -> DocumentTranscription:
    transcription = DocumentTranscription(
        id=str(uuid.uuid4()),
        title="Second Transcription",
        content="Another transcription for testing.",
        project_id=sample_project.id,
    )
    db_session.add(transcription)
    db_session.commit()
    db_session.refresh(transcription)
    return transcription


@pytest.fixture
def sample_second_user_story(
    db_session: Session,
    sample_project: Project,
    sample_user: User,
    sample_transcription: DocumentTranscription,
) -> UserStory:
    story = UserStory(
        id=str(uuid.uuid4()),
        name="Second User Story",
        description="As a tester I want more coverage",
        points=5,
        is_done=True,
        project_id=sample_project.id,
        user_id=sample_user.id,
        transcription_id=sample_transcription.id,
        log="completed",
    )
    db_session.add(story)
    db_session.commit()
    db_session.refresh(story)
    return story


@pytest.fixture
def sample_user_story_dev_step(
    db_session: Session, sample_user_story: UserStory
) -> UserStoryDevStep:
    step = UserStoryDevStep(
        id=str(uuid.uuid4()),
        order=1,
        name="Setup environment",
        description="Install dependencies and configure the environment",
        user_story_id=sample_user_story.id,
    )
    db_session.add(step)
    db_session.commit()
    db_session.refresh(step)
    return step


@pytest.fixture
def sample_second_dev_step(
    db_session: Session, sample_user_story: UserStory
) -> UserStoryDevStep:
    step = UserStoryDevStep(
        id=str(uuid.uuid4()),
        order=2,
        name="Implement feature",
        description="Write the code for the feature",
        user_story_id=sample_user_story.id,
    )
    db_session.add(step)
    db_session.commit()
    db_session.refresh(step)
    return step


@pytest.fixture
def sample_project_repository(
    db_session: Session, sample_project: Project
) -> ProjectRepository:
    org = Organization(
        id=str(uuid.uuid4()),
        name="Test Org",
    )
    db_session.add(org)
    db_session.commit()

    installation = OrganizationGitHubAppInstallation(
        id=str(uuid.uuid4()),
        github_app_installation_id=12345,
        organization_id=org.id,
    )
    db_session.add(installation)
    db_session.commit()

    repo = ProjectRepository(
        id=str(uuid.uuid4()),
        project_id=sample_project.id,
        url="https://github.com/example/test-repo",
        name="test-repo",
        owner="example",
        language="Python",
        full_name="example/test-repo",
        default_branch="main",
        organization_github_installation_id=installation.id,
    )
    db_session.add(repo)
    db_session.commit()
    db_session.refresh(repo)
    return repo


@pytest.fixture
def sample_second_tag(db_session: Session, sample_project: Project) -> Tag:
    tag = Tag(
        id=str(uuid.uuid4()),
        project_id=sample_project.id,
        tag="integration",
    )
    db_session.add(tag)
    db_session.commit()
    db_session.refresh(tag)
    return tag


@pytest.fixture
def sample_user_story_tag(
    db_session: Session, sample_user_story: UserStory, sample_tag: Tag
) -> UserStoryTag:
    link = UserStoryTag(
        id=str(uuid.uuid4()),
        user_story_id=sample_user_story.id,
        tag_id=sample_tag.id,
    )
    db_session.add(link)
    db_session.commit()
    db_session.refresh(link)
    return link


@pytest.fixture
def sample_tech_spec_tag(
    db_session: Session, sample_tech_spec: DocumentTechSpec, sample_tag: Tag
) -> TechSpecTag:
    link = TechSpecTag(
        id=str(uuid.uuid4()),
        tech_spec_id=sample_tech_spec.id,
        tag_id=sample_tag.id,
    )
    db_session.add(link)
    db_session.commit()
    db_session.refresh(link)
    return link


@pytest.fixture
def sample_transcription_tag(
    db_session: Session,
    sample_transcription: DocumentTranscription,
    sample_tag: Tag,
) -> TranscriptionTag:
    link = TranscriptionTag(
        id=str(uuid.uuid4()),
        transcription_id=sample_transcription.id,
        tag_id=sample_tag.id,
    )
    db_session.add(link)
    db_session.commit()
    db_session.refresh(link)
    return link


@pytest.fixture
def sample_tool(db_session: Session) -> Tool:
    tool = Tool(
        name="get_full_project_info",
        description="Fetch full project information",
    )
    db_session.add(tool)
    db_session.commit()
    db_session.refresh(tool)
    return tool


@pytest.fixture
def sample_second_tool(db_session: Session) -> Tool:
    tool = Tool(
        name="get_stories_by_input_tags",
        description="Fetch related user stories by tags",
    )
    db_session.add(tool)
    db_session.commit()
    db_session.refresh(tool)
    return tool


@pytest.fixture
def sample_context(db_session: Session) -> Context:
    ctx = Context(
        name="get_full_project_info",
        description="Fetch full project information for a given project ID.",
    )
    db_session.add(ctx)
    db_session.commit()
    db_session.refresh(ctx)
    return ctx


@pytest.fixture
def sample_mcp(db_session: Session) -> Mcp:
    mcp = Mcp(
        name="test-mcp-server",
        description="A test MCP server",
    )
    db_session.add(mcp)
    db_session.commit()
    db_session.refresh(mcp)
    return mcp


@pytest.fixture
def sample_agent(db_session: Session) -> Agent:
    agent = Agent(
        id=str(uuid.uuid4()),
        name="transcription_to_user_story",
        model="gpt-4o",
        is_default=True,
        user_prompt="Generate user stories from transcriptions",
        system_prompt="Generate user stories from transcriptions",
        temperature=0.7,
        max_retries=3,
    )
    db_session.add(agent)
    db_session.commit()
    db_session.refresh(agent)
    return agent


@pytest.fixture
def sample_second_agent(db_session: Session) -> Agent:
    agent = Agent(
        id=str(uuid.uuid4()),
        name="labeler_agent",
        model="gpt-4o-mini",
        is_default=False,
        user_prompt="Label user stories and tech specs",
        system_prompt="Label user stories and tech specs",
        temperature=0.3,
        max_retries=2,
    )
    db_session.add(agent)
    db_session.commit()
    db_session.refresh(agent)
    return agent


@pytest.fixture
def sample_agent_tool(
    db_session: Session, sample_agent: Agent, sample_tool: Tool
) -> AgentTool:
    link = AgentTool(
        id=str(uuid.uuid4()),
        agent_id=sample_agent.id,
        tool_name=sample_tool.name,
    )
    db_session.add(link)
    db_session.commit()
    db_session.refresh(link)
    return link


@pytest.fixture
def sample_agent_context(
    db_session: Session, sample_agent: Agent, sample_context: Context
) -> AgentContext:
    link = AgentContext(
        agent_id=sample_agent.id,
        context_name=sample_context.name,
    )
    db_session.add(link)
    db_session.commit()
    db_session.refresh(link)
    return link


@pytest.fixture
def sample_agent_mcp(
    db_session: Session, sample_agent: Agent, sample_mcp: Mcp
) -> AgentMcp:
    link = AgentMcp(
        agent_id=sample_agent.id,
        mcp_name=sample_mcp.name,
    )
    db_session.add(link)
    db_session.commit()
    db_session.refresh(link)
    return link


@pytest.fixture
def sample_project_agent(
    db_session: Session, sample_project: Project, sample_agent: Agent
) -> ProjectAgent:
    pa = ProjectAgent(
        id=str(uuid.uuid4()),
        project_id=sample_project.id,
        agent_name=sample_agent.name,
        agent_id=sample_agent.id,
    )
    db_session.add(pa)
    db_session.commit()
    db_session.refresh(pa)
    return pa


@pytest.fixture
def sample_agent_run(
    db_session: Session, sample_user: User, sample_project: Project
) -> AgentRun:
    run = AgentRun(
        id=str(uuid.uuid4()),
        user_id=sample_user.id,
        project_id=sample_project.id,
        title="Test Agent Run",
        status="completed",
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run


@pytest.fixture
def sample_agent_log(
    db_session: Session, sample_agent_run: AgentRun
) -> AgentLog:
    log = AgentLog(
        id=str(uuid.uuid4()),
        agent_run_id=sample_agent_run.id,
        message="Agent started processing transcription",
    )
    db_session.add(log)
    db_session.commit()
    db_session.refresh(log)
    return log


@pytest.fixture
def sample_second_agent_log(
    db_session: Session, sample_agent_run: AgentRun
) -> AgentLog:
    log = AgentLog(
        id=str(uuid.uuid4()),
        agent_run_id=sample_agent_run.id,
        message="Agent finished processing transcription",
    )
    db_session.add(log)
    db_session.commit()
    db_session.refresh(log)
    return log


@pytest.fixture
def sample_user_access_code(
    db_session: Session, sample_user: User
) -> UserAccessCode:
    from datetime import datetime, timedelta, timezone

    code = UserAccessCode(
        id=str(uuid.uuid4()),
        user_email=sample_user.email,
        access_code="1234",
        access_code_expiry=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db_session.add(code)
    db_session.commit()
    db_session.refresh(code)
    return code


@pytest.fixture
def sample_second_gherkin_rule(
    db_session: Session, sample_user_story: UserStory
) -> GherkinRule:
    rule = GherkinRule(
        id=str(uuid.uuid4()),
        index=1,
        title="Second Gherkin Rule",
        background="Given an alternative scenario",
        user_story_id=sample_user_story.id,
    )
    db_session.add(rule)
    db_session.commit()
    db_session.refresh(rule)
    return rule


@pytest.fixture
def sample_second_gherkin_scenario(
    db_session: Session, sample_gherkin_rule: GherkinRule
) -> GherkinScenario:
    scenario = GherkinScenario(
        id=str(uuid.uuid4()),
        index=1,
        title="Second Gherkin Scenario",
        text="When I run edge-case tests Then they should handle errors",
        is_done=True,
        gherkin_rule_id=sample_gherkin_rule.id,
    )
    db_session.add(scenario)
    db_session.commit()
    db_session.refresh(scenario)
    return scenario
