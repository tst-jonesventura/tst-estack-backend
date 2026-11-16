"""
Main FastAPI application module.
"""

import os

import newrelic.agent
from routers.webhooks import jira_router

if os.path.exists("newrelic.ini"):
    try:
        newrelic.agent.initialize("newrelic.ini", ignore_errors=True)
    except Exception:
        pass

import logging
from contextlib import asynccontextmanager

from agents.utils.context import sync_context
from agents.utils.mcps import sync_mcps
from agents.utils.tools import sync_tools
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routers import system_router
from routers.crud import integration_router, websocket_router
from services.notification_service import notification_service

from .database.database import create_session, get_pool_status
from .routers.agents import (
    create_development_session_router,
    labeler_agent_router,
    openhands_chat_router,
    repo_to_tech_spec_router,
    transcription_to_tech_spec_router,
    transcription_to_user_story_router,
    tutor_router,
    user_story_to_dev_steps_router,
)
from .routers.crud import (
    agent_router,
    agent_run_router,
    context_router,
    database_connector_router,
    gherkin_rule_router,
    gherkin_scenario_router,
    github_apps_router,
    notification_router,
    project_agent_router,
    project_database_router,
    project_kanban_router,
    project_kanban_rule_router,
    project_repository_router,
    project_router,
    tool_router,
    user_project_router,
    user_router,
    user_story_branch_router,
    user_story_dev_step_router,
    user_story_router,
)
from .routers.documents import tech_spec_router, transcription_router
from .utils import log
from .utils.config import settings

logger = log.setup_custom_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    with create_session() as session:
        sync_context(session)
        sync_tools(session)
        sync_mcps(session)
        notification_service.purge_old_notifications(session)

    yield


app = FastAPI(
    title="EStack Backend API",
    description="Backend API with OpenAI integration for project creation",
    version="0.0.1",
    lifespan=lifespan,
)

# Disable logging for specific endpoints
log_filter = log.EndpointFilter("/websocket/notifications")
logging.getLogger("uvicorn.access").addFilter(log_filter)
logging.getLogger("uvicorn.error").addFilter(log_filter)

cors_origins = settings.cors_origins.split(",")
cors_origins = [origin.strip() for origin in cors_origins]

app.add_middleware(
    CORSMiddleware,
    allow_origins=[],  # Empty list disables static check
    allow_origin_regex=".*",  # Allows any origin
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(
    prefix="",
    tags=["system"],
    router=system_router.router,
)

app.include_router(
    prefix="/projects",
    tags=["projects"],
    router=project_router.router,
)

app.include_router(
    prefix="/projects/{project_id}",
    tags=["project_agents"],
    router=project_agent_router.router,
)

app.include_router(
    prefix="/projects/{project_id}/user_stories",
    tags=["user_stories"],
    router=user_story_router.router,
)

app.include_router(
    prefix="/projects/{project_id}/documents",
    tags=["documents"],
    router=transcription_router.router,
)

app.include_router(
    prefix="/projects/{project_id}/documents/tech-specs",
    tags=["tech-specs"],
    router=tech_spec_router.router,
)

app.include_router(
    prefix="/projects/{project_id}/user-projects",
    tags=["user-projects"],
    router=user_project_router.router,
)

app.include_router(
    prefix="/projects/{project_id}/agent_runs",
    tags=["agent_runs"],
    router=agent_run_router.router,
)

app.include_router(
    prefix="/projects/{project_id}/user_stories/{user_story_id}/dev_steps",
    tags=["user_story_dev_steps"],
    router=user_story_dev_step_router.router,
)

app.include_router(
    prefix="/projects/{project_id}/user_stories/{user_story_id}/gherkin_rules",
    tags=["gherkin_rules"],
    router=gherkin_rule_router.router,
)

app.include_router(
    prefix="/projects/{project_id}/user_stories/{user_story_id}/gherkin_rules/{rule_id}/scenarios",
    tags=["gherkin_scenarios"],
    router=gherkin_scenario_router.router,
)

app.include_router(
    prefix="/projects/{project_id}/repositories",
    tags=["project_repositories"],
    router=project_repository_router.router,
)

app.include_router(
    prefix="/projects/{project_id}/databases",
    tags=["project_databases"],
    router=project_database_router.router,
)

app.include_router(
    prefix="/projects/{project_id}/database-connectors",
    tags=["database_connectors"],
    router=database_connector_router.project_router,
)

app.include_router(
    prefix="/database-connectors",
    tags=["database_connectors"],
    router=database_connector_router.overview_router,
)

app.include_router(
    prefix="/organizations/{organization_id}/database-connectors",
    tags=["database_connectors"],
    router=database_connector_router.admin_router,
)

app.include_router(
    prefix="/connector",
    tags=["connector"],
    router=database_connector_router.connector_router,
)

app.include_router(
    prefix="/projects/{project_id}/user_story_branches",
    tags=["user_story_branches"],
    router=user_story_branch_router.router,
)

app.include_router(
    prefix="/projects/{project_id}/integrations",
    tags=["integrations"],
    router=integration_router.router,
)

app.include_router(
    prefix="/agents",
    tags=["agents"],
    router=agent_router.router,
)

app.include_router(
    prefix="/user_story_to_dev_steps",
    tags=["user_story_to_dev_steps"],
    router=user_story_to_dev_steps_router.router,
)

app.include_router(
    prefix="/create_development_session",
    tags=["create_development_session"],
    router=create_development_session_router.router,
)

app.include_router(
    prefix="/tutor",
    tags=["tutor"],
    router=tutor_router.router,
)

app.include_router(
    prefix="",
    tags=["tools"],
    router=tool_router.router,
)
app.include_router(
    prefix="",
    tags=["contexts"],
    router=context_router.router,
)

app.include_router(
    prefix="/transcription_to_user_stories",
    tags=["transcription_to_user_stories"],
    router=transcription_to_user_story_router.router,
)

app.include_router(
    prefix="/transcription_to_tech_spec",
    tags=["transcription_to_tech_spec"],
    router=transcription_to_tech_spec_router.router,
)
app.include_router(
    prefix="/label",
    tags=["labeler_agent"],
    router=labeler_agent_router.router,
)
app.include_router(
    prefix="/repo_to_tech_spec",
    tags=["repo_to_tech_spec"],
    router=repo_to_tech_spec_router.router,
)
app.include_router(
    prefix="/projects/{project_id}/user_stories/{user_story_id}/dev_steps",
    tags=["user_story_dev_steps"],
    router=user_story_dev_step_router.router,
)
app.include_router(
    prefix="/projects/{project_id}/user_stories/{user_story_id}/gherkin_rules",
    tags=["gherkin_rules"],
    router=gherkin_rule_router.router,
)
app.include_router(
    prefix="/projects/{project_id}/user_stories/{user_story_id}/gherkin_rules/{rule_id}/scenarios",
    tags=["gherkin_scenarios"],
    router=gherkin_scenario_router.router,
)

app.include_router(
    prefix="/openhands/conversations",
    tags=["openhands_chat"],
    router=openhands_chat_router.router,
)

# Add ProjectRepository CRUD route
app.include_router(
    prefix="/projects/{project_id}/repositories",
    tags=["project_repositories"],
    router=project_repository_router.router,
)
app.include_router(
    prefix="/projects/{project_id}/user_story_branches",
    tags=["user_story_branches"],
    router=user_story_branch_router.router,
)
app.include_router(
    prefix="/users",
    tags=["users"],
    router=user_router.router,
)

app.include_router(
    prefix="/project_kanban",
    tags=["project_kanban"],
    router=project_kanban_router.router,
)

app.include_router(
    prefix="/project_kanban_rules",
    tags=["project_kanban_rules"],
    router=project_kanban_rule_router.router,
)

app.include_router(
    prefix="/github_app",
    tags=["github_app_installations"],
    router=github_apps_router.router,
)

app.include_router(
    prefix="/notifications",
    tags=["notifications"],
    router=notification_router.router,
)

app.include_router(
    prefix="/websocket",
    tags=["websocket"],
    router=websocket_router.router,
)

app.include_router(
    prefix="/webhook/jira",
    tags=["webhook", "jira"],
    router=jira_router.router,
)


@app.get("/")
async def root():
    """Root endpoint."""
    return {"message": "EStack Backend API is running"}


@app.get("/health/db-pool", tags=["health"])
async def db_pool_health():
    """Return current database connection pool usage metrics."""
    return get_pool_status()
