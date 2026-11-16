"""Idempotent initial database seeding script for e-Stack.

Populates baseline data required for the application to be operational immediately:
- AI Registries (tools, contexts, MCP providers)
- Default AI Agents (ask_git, tutor)
- Initial Roles (owner, admin, contributor, viewer)
- Default Organization and Administrative User(s)
- Default Project with Kanban Columns and User-Project role bindings

Can be executed safely on every container restart.

Run from the ``src`` directory:
    python -m scripts.seed_initial_data
"""

import json
import os
from pathlib import Path

from agents.utils.context import sync_context
from agents.utils.mcps import sync_mcps
from agents.utils.tools import sync_tools
from database import models
from database.database import create_session
from scripts import seed_ask_git_agent, seed_tutor_agent
from sqlalchemy import text
from utils import log
from utils.config import settings

logger = log.setup_custom_logger(__name__)


# Configurable environment defaults
def get_default_admin_email() -> str:
    """Get configured admin email from environment variables, falling back to default."""
    return (
        os.getenv("SEED_ADMIN_EMAIL", "").strip()
        or os.getenv("ADMIN_EMAIL", "").strip()
        or "admin@elumini-it.com"
    )


DEFAULT_ADMIN_EMAIL = get_default_admin_email()
SYSTEM_ESTACK_EMAIL = "e-stack@elumini-it.com"
DEFAULT_ORG_NAME = (
    os.getenv("SEED_DEFAULT_ORG") or settings.default_organization_name or "Elumini-IT"
)
DEFAULT_PROJECT_NAME = os.getenv("SEED_DEFAULT_PROJECT", "Default Project")
DEFAULT_MODEL = "openai:gpt-5.6-terra"
DEFAULT_AGENT_SEED_PATH = "_PRD_estack_agent_202610011722.json"

INITIAL_ROLES = [
    {"name": "owner", "description": "Project Owner"},
    {"name": "admin", "description": "Administrator"},
    {"name": "contributor", "description": "Contributor"},
    {"name": "viewer", "description": "Viewer"},
]


def seed_registries() -> None:
    """Sync tools, contexts, and MCP entries with database."""
    logger.info("Syncing tools, contexts, and MCP registries...")
    with create_session() as session:
        sync_tools(session)
    with create_session() as session:
        sync_context(session)
    with create_session() as session:
        sync_mcps(session)
    logger.info("AI registries synced successfully.")


DEFAULT_SYSTEM_AGENTS = [
    {
        "name": "user_story_to_dev_steps",
        "model": DEFAULT_MODEL,
        "temperature": 0.1,
        "max_retries": 3,
        "is_default": True,
    },
    {
        "name": "tutor",
        "model": DEFAULT_MODEL,
        "temperature": 0.1,
        "max_retries": 3,
        "is_default": True,
    },
    {
        "name": "code_git",
        "model": DEFAULT_MODEL,
        "temperature": 0.1,
        "max_retries": 3,
        "is_default": True,
    },
    {
        "name": "transcription_to_story",
        "model": DEFAULT_MODEL,
        "temperature": 0.1,
        "max_retries": 3,
        "is_default": True,
    },
    {
        "name": "gherkin",
        "model": DEFAULT_MODEL,
        "temperature": 0.1,
        "max_retries": 3,
        "is_default": True,
    },
    {
        "name": "devin-api",
        "model": DEFAULT_MODEL,
        "temperature": 0.1,
        "max_retries": 3,
        "is_default": True,
    },
    {
        "name": "transcription_to_tech_spec",
        "model": DEFAULT_MODEL,
        "temperature": 0.1,
        "max_retries": 3,
        "is_default": True,
    },
    {
        "name": "ask_git",
        "model": DEFAULT_MODEL,
        "temperature": 0.1,
        "max_retries": 3,
        "is_default": True,
    },
    {
        "name": "repo_to_tech_spec",
        "model": DEFAULT_MODEL,
        "temperature": 0.1,
        "max_retries": 3,
        "is_default": True,
        "mcps": ["git_mcp"],
    },
    {
        "name": "labeler",
        "model": DEFAULT_MODEL,
        "temperature": 0.1,
        "max_retries": 3,
        "is_default": True,
    },
    {
        "name": "unit_test_agent",
        "model": DEFAULT_MODEL,
        "temperature": 0.1,
        "max_retries": 3,
        "is_default": True,
    },
]


def _load_agents_json() -> list[dict]:
    """Find and load the agents JSON configuration file, falling back to DEFAULT_SYSTEM_AGENTS."""
    candidates = [
        Path.cwd() / DEFAULT_AGENT_SEED_PATH,
        Path(DEFAULT_AGENT_SEED_PATH),
        Path("/app/_PRD_estack_agent_202610011722.json"),
        Path(__file__).resolve().parent.parent.parent / DEFAULT_AGENT_SEED_PATH,
        Path(__file__).resolve().parent.parent / DEFAULT_AGENT_SEED_PATH,
        Path(__file__).resolve().parent / DEFAULT_AGENT_SEED_PATH,
    ]
    for path in candidates:
        if path.is_file():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    agents = data.get("agent", [])
                    if agents:
                        return agents
            except Exception as e:
                logger.warning("Error reading agents JSON from %s: %s", path, e)
    return DEFAULT_SYSTEM_AGENTS


def _apply_agent_attributes(agent: models.Agent, item: dict, agent_name: str) -> None:
    """Set prompt and hyperparameter attributes on agent instance."""
    agent.model = item.get("model", DEFAULT_MODEL)
    agent.temperature = item.get("temperature", 0.1)
    agent.max_retries = item.get("max_retries", 3)
    agent.user_prompt = item.get("user_prompt")
    agent.system_prompt = item.get("system_prompt")

    if agent_name == seed_ask_git_agent.ASK_GIT_NAME:
        agent.system_prompt = seed_ask_git_agent.ASK_GIT_SYSTEM_PROMPT
        agent.user_prompt = None
        agent.temperature = seed_ask_git_agent.ASK_GIT_TEMPERATURE
    elif agent_name == seed_tutor_agent.TUTOR_NAME:
        agent.system_prompt = seed_tutor_agent.TUTOR_SYSTEM_PROMPT
        agent.user_prompt = None
        agent.temperature = seed_tutor_agent.TUTOR_TEMPERATURE


def _sync_agent_mcps(session, agent_id: int, item: dict, agent_name: str) -> None:
    """Synchronize MCP associations for an agent."""
    mcps = item.get("mcps")
    if mcps:
        session.query(models.AgentMcp).filter_by(agent_id=agent_id).delete()
        for mcp_name in mcps:
            session.add(models.AgentMcp(agent_id=agent_id, mcp_name=mcp_name))
        session.commit()
    elif agent_name == "repo_to_tech_spec":
        has_git_mcp = (
            session.query(models.AgentMcp)
            .filter_by(agent_id=agent_id, mcp_name="git_mcp")
            .first()
        )
        if not has_git_mcp:
            session.add(models.AgentMcp(agent_id=agent_id, mcp_name="git_mcp"))
            session.commit()


def _sync_agent_tools(session, agent_id: int, item: dict) -> None:
    """Synchronize tool associations for an agent."""
    tools = item.get("tools")
    if tools:
        session.query(models.AgentTool).filter_by(agent_id=agent_id).delete()
        for tool_name in tools:
            session.add(models.AgentTool(agent_id=agent_id, tool_name=tool_name))
        session.commit()


def _upsert_agent(session, item: dict, agent_name: str) -> models.Agent:
    """Find or create an Agent record and persist updated attributes."""
    agent = (
        session.query(models.Agent)
        .filter(
            models.Agent.name == agent_name,
            models.Agent.is_default == True,
        )
        .one_or_none()
    )

    if agent is None:
        agent = models.Agent(
            name=agent_name,
            is_default=bool(item.get("is_default", 1)),
        )
        session.add(agent)

    _apply_agent_attributes(agent, item, agent_name)
    session.commit()
    session.refresh(agent)
    return agent


def _process_single_agent(session, item: dict) -> None:
    """Process upsert and relation sync for a single agent definition."""
    agent_name = item.get("name")
    if not agent_name:
        return

    agent = _upsert_agent(session, item, agent_name)
    _sync_agent_mcps(session, agent.id, item, agent_name)
    _sync_agent_tools(session, agent.id, item)
    logger.info("Seeded/updated agent '%s' (id=%s).", agent.name, agent.id)


def seed_default_agents() -> None:
    """Seed all default agents (ask_git, tutor and all agents from JSON definition)."""
    logger.info("Seeding default agents...")

    # 1. First run dedicated agent seeds which configure tools/mcps/contexts and overrides
    seed_ask_git_agent.seed()
    seed_tutor_agent.seed()

    # 2. Seed/update all agents defined in _PRD_estack_agent_202610011722.json
    agents_data = _load_agents_json()
    if not agents_data:
        logger.warning("No agents found in JSON definition file.")
        logger.info("Default agents seeded successfully.")
        return

    logger.info("Seeding %d agents from JSON definition...", len(agents_data))
    with create_session() as session:
        for item in agents_data:
            _process_single_agent(session, item)

    logger.info("Default agents seeded successfully.")


def seed_roles() -> dict[str, models.Role]:
    """Seed system roles if not present and return a mapping of name to Role."""
    logger.info("Seeding system roles...")
    roles = {}
    with create_session() as session:
        for r_data in INITIAL_ROLES:
            role = (
                session.query(models.Role)
                .filter(models.Role.name == r_data["name"])
                .one_or_none()
            )
            if role is None:
                role = models.Role(
                    name=r_data["name"],
                    description=r_data["description"],
                )
                session.add(role)
                session.commit()
                session.refresh(role)
                logger.info("Created role: '%s'", role.name)
            roles[role.name] = role
    logger.info("System roles validated.")
    return roles


def seed_organization() -> models.Organization:
    """Seed default organization if not present."""
    logger.info("Seeding default organization...")
    with create_session() as session:
        org = (
            session.query(models.Organization)
            .filter(models.Organization.name == DEFAULT_ORG_NAME)
            .one_or_none()
        )
        if org is None:
            org = models.Organization(
                name=DEFAULT_ORG_NAME,
                description="Default Organization for e-Stack",
                is_active=True,
            )
            session.add(org)
            session.commit()
            session.refresh(org)
            logger.info("Created default organization: '%s' (id=%s)", org.name, org.id)
        else:
            logger.info(
                "Default organization '%s' already exists (id=%s).", org.name, org.id
            )
        return org


def seed_users() -> tuple[models.User, models.User]:
    """Seed initial administrative user and system e-stack user."""
    admin_email = get_default_admin_email()
    logger.info("Seeding users (admin email: '%s')...", admin_email)
    with create_session() as session:
        # 1. Admin user
        admin_user = (
            session.query(models.User)
            .filter(models.User.email == admin_email)
            .one_or_none()
        )
        if admin_user is None:
            admin_user = models.User(
                email=admin_email,
                is_active=True,
                is_admin=True,
            )
            session.add(admin_user)
            session.commit()
            session.refresh(admin_user)
            logger.info(
                "Created admin user: '%s' (id=%s)", admin_user.email, admin_user.id
            )
        else:
            if not admin_user.is_admin or not admin_user.is_active:
                admin_user.is_admin = True
                admin_user.is_active = True
                session.commit()
                session.refresh(admin_user)
            logger.info(
                "Admin user '%s' already exists (id=%s).",
                admin_user.email,
                admin_user.id,
            )

        # 2. System e-stack user (used by background automation / agents)
        estack_user = (
            session.query(models.User)
            .filter(models.User.email == SYSTEM_ESTACK_EMAIL)
            .one_or_none()
        )
        if estack_user is None:
            estack_user = models.User(
                email=SYSTEM_ESTACK_EMAIL,
                is_active=True,
                is_admin=True,
            )
            session.add(estack_user)
            session.commit()
            session.refresh(estack_user)
            logger.info(
                "Created system user: '%s' (id=%s)", estack_user.email, estack_user.id
            )
        else:
            logger.info(
                "System user '%s' already exists (id=%s).",
                estack_user.email,
                estack_user.id,
            )

        return admin_user, estack_user


def seed_project_and_kanban(
    org: models.Organization,
    admin_user: models.User,
    estack_user: models.User,
    roles: dict[str, models.Role],
) -> models.Project:
    """Seed default project, user-project associations, and Kanban columns."""
    logger.info("Seeding default project and Kanban board...")
    with create_session() as session:
        project = (
            session.query(models.Project)
            .filter(
                models.Project.name == DEFAULT_PROJECT_NAME,
                models.Project.organization_id == org.id,
            )
            .one_or_none()
        )
        if project is None:
            project = models.Project(
                name=DEFAULT_PROJECT_NAME,
                description="Default workspace project",
                is_active=True,
                organization_id=org.id,
            )
            session.add(project)
            session.commit()
            session.refresh(project)
            logger.info(
                "Created default project: '%s' (id=%s)", project.name, project.id
            )
        else:
            logger.info(
                "Default project '%s' already exists (id=%s).", project.name, project.id
            )

        # Ensure Kanban columns exist for the project
        existing_columns = (
            session.query(models.ProjectKanbanColumn)
            .filter(models.ProjectKanbanColumn.project_id == project.id)
            .all()
        )
        if not existing_columns:
            columns_str = settings.default_kanban_columns
            column_names = [
                col.strip() for col in columns_str.split(",") if col.strip()
            ]
            for order, col_name in enumerate(column_names):
                column = models.ProjectKanbanColumn(
                    name=col_name,
                    project_id=project.id,
                    position=order,
                )
                session.add(column)
            session.commit()
            logger.info(
                "Created %d Kanban columns for project '%s'.",
                len(column_names),
                project.name,
            )
        else:
            logger.info(
                "Project '%s' already has %d Kanban columns.",
                project.name,
                len(existing_columns),
            )

        # Ensure Admin user has owner role on the project
        owner_role = session.query(models.Role).filter(
            models.Role.name == "owner"
        ).one_or_none() or roles.get("owner")
        if owner_role:
            admin_link = (
                session.query(models.UserProject)
                .filter(
                    models.UserProject.user_id == admin_user.id,
                    models.UserProject.project_id == project.id,
                )
                .one_or_none()
            )
            if admin_link is None:
                admin_link = models.UserProject(
                    user_id=admin_user.id,
                    project_id=project.id,
                    role_id=owner_role.id,
                )
                session.add(admin_link)
                session.commit()
                logger.info(
                    "Linked admin '%s' as owner of project '%s'.",
                    admin_user.email,
                    project.name,
                )

        # Ensure e-stack system user has contributor role on the project
        contributor_role = session.query(models.Role).filter(
            models.Role.name == "contributor"
        ).one_or_none() or roles.get("contributor")
        if contributor_role and estack_user.id != admin_user.id:
            estack_link = (
                session.query(models.UserProject)
                .filter(
                    models.UserProject.user_id == estack_user.id,
                    models.UserProject.project_id == project.id,
                )
                .one_or_none()
            )
            if estack_link is None:
                estack_link = models.UserProject(
                    user_id=estack_user.id,
                    project_id=project.id,
                    role_id=contributor_role.id,
                )
                session.add(estack_link)
                session.commit()
                logger.info(
                    "Linked system user '%s' as contributor of project '%s'.",
                    estack_user.email,
                    project.name,
                )

        return project


def ensure_database_sequences() -> None:
    """Ensure database sequences like transaction_id_seq exist for versioning (Continuum)."""
    logger.info("Verifying database sequences...")
    try:
        with create_session() as session:
            bind = session.get_bind()
            if bind and bind.dialect.name == "mssql":
                session.execute(
                    text(
                        """
                        IF NOT EXISTS (SELECT * FROM sys.sequences WHERE name = 'transaction_id_seq')
                        BEGIN
                            CREATE SEQUENCE transaction_id_seq START WITH 1 INCREMENT BY 1;
                        END
                        """
                    )
                )
                session.commit()
                logger.info("Sequence 'transaction_id_seq' verified/created.")
    except Exception as e:
        logger.warning("Could not verify sequence transaction_id_seq: %s", e)


def seed_all() -> None:
    """Execute complete initial seeding process."""
    logger.info("Starting database initial seeding...")
    ensure_database_sequences()
    seed_registries()
    seed_default_agents()
    roles = seed_roles()
    org = seed_organization()
    admin_user, estack_user = seed_users()
    seed_project_and_kanban(org, admin_user, estack_user, roles)
    logger.info("Database initial seeding completed successfully!")


if __name__ == "__main__":
    seed_all()
