"""Tests for the initial database seeding script (seed_initial_data)."""

from contextlib import contextmanager

import pytest
from database import models
from scripts import seed_initial_data as script


@pytest.fixture
def mock_dependencies(db_session, monkeypatch):
    @contextmanager
    def _factory():
        yield db_session

    monkeypatch.setattr(script, "create_session", _factory)
    monkeypatch.setattr(script.seed_ask_git_agent, "create_session", _factory)
    monkeypatch.setattr(script.seed_tutor_agent, "create_session", _factory)
    # Mock MCPs for AgentMcp foreign key constraint
    for mcp_name in ["git_mcp", "ask_devin"]:
        if not db_session.query(models.Mcp).filter_by(name=mcp_name).first():
            db_session.add(models.Mcp(name=mcp_name, description="Test MCP"))
    # Mock Tools for AgentTool foreign key constraint
    for tool_name in ["ask_git", "get_stories", "get_tech_specs"]:
        if not db_session.query(models.Tool).filter_by(name=tool_name).first():
            db_session.add(models.Tool(name=tool_name, description="Test Tool"))
    # Mock Contexts for AgentContext foreign key constraint
    for ctx_name in [
        "get_full_project_info",
        "get_all_specs",
        "get_all_technical_details",
        "get_done_stories_by_project",
        "list_project_tags",
    ]:
        if not db_session.query(models.Context).filter_by(name=ctx_name).first():
            db_session.add(models.Context(name=ctx_name, description="Test Context"))
    db_session.commit()


class TestSeedInitialData:
    def test_seed_roles(self, db_session, mock_dependencies):
        roles = script.seed_roles()
        assert "owner" in roles
        assert "admin" in roles
        assert "contributor" in roles
        assert "viewer" in roles

        db_roles = db_session.query(models.Role).all()
        assert len(db_roles) == 4

    def test_seed_organization(self, db_session, mock_dependencies):
        org = script.seed_organization()
        assert org is not None
        assert org.name == script.DEFAULT_ORG_NAME

        # Calling again returns same org and does not duplicate
        org_again = script.seed_organization()
        assert org_again.id == org.id
        assert db_session.query(models.Organization).count() == 1

    def test_seed_users(self, db_session, mock_dependencies):
        admin, estack = script.seed_users()
        assert admin.email == script.get_default_admin_email()
        assert admin.is_admin is True
        assert estack.email == script.SYSTEM_ESTACK_EMAIL
        assert estack.is_admin is True

        # Idempotency
        admin_again, estack_again = script.seed_users()
        assert admin_again.id == admin.id
        assert estack_again.id == estack.id
        assert db_session.query(models.User).count() == 2

    def test_seed_users_custom_admin_email(
        self, db_session, mock_dependencies, monkeypatch
    ):
        monkeypatch.setenv("SEED_ADMIN_EMAIL", "custom_admin@mycompany.com")
        admin, _ = script.seed_users()
        assert admin.email == "custom_admin@mycompany.com"
        assert admin.is_admin is True
        assert admin.is_active is True

    def test_seed_project_and_kanban(self, db_session, mock_dependencies):
        roles = script.seed_roles()
        org = script.seed_organization()
        admin, estack = script.seed_users()

        project = script.seed_project_and_kanban(org, admin, estack, roles)
        assert project.name == script.DEFAULT_PROJECT_NAME
        assert project.organization_id == org.id

        # Verify Kanban columns
        columns = (
            db_session.query(models.ProjectKanbanColumn)
            .filter_by(project_id=project.id)
            .order_by(models.ProjectKanbanColumn.position.asc())
            .all()
        )
        assert len(columns) > 0
        assert columns[0].position == 0

        # Verify user project roles
        user_projects = (
            db_session.query(models.UserProject).filter_by(project_id=project.id).all()
        )
        user_ids = {up.user_id for up in user_projects}
        assert admin.id in user_ids
        assert estack.id in user_ids

    def test_seed_all_idempotent(self, db_session, mock_dependencies):
        script.seed_all()
        script.seed_all()

        assert db_session.query(models.Organization).count() == 1
        assert db_session.query(models.Project).count() == 1
        assert db_session.query(models.User).count() == 2
        assert db_session.query(models.Role).count() == 4
        assert db_session.query(models.UserProject).count() == 2
        # Verify agents from JSON are seeded
        assert db_session.query(models.Agent).count() >= 11
        user_story_agent = (
            db_session.query(models.Agent)
            .filter_by(name="user_story_to_dev_steps")
            .first()
        )
        assert user_story_agent is not None
        assert user_story_agent.model == "openai:gpt-5.6-terra"
        assert user_story_agent.is_default is True
