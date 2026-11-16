from datetime import datetime, timezone

from agents import code_agent, gherkin_agent, openhands_agent, unit_test_agent
from agents.utils.agent_deps import AgentDeps
from agents.utils.agent_factory import create_agent_from_database
from agents.utils.agent_run_utils import create_agent_logger
from database.database import create_session
from database.models import (
    GherkinRule,
    ProjectRepository,
    User,
    UserStory,
    UserStoryDevStep,
)
from schemas.openhands_schemas import OpenHandsRepository
from schemas.project_schemas import ProjectRepositoryPublic
from schemas.user_story_schemas import (
    GherkinRuleFullToAgentRunPublic,
    UserStoryDevStepBase,
    UserStoryDevStepPublic,
    UserStoryPublic,
)
from sqlalchemy.orm import joinedload
from utils import log

logger = log.setup_custom_logger(__name__)


NO_REPOS_FOUND_MSG = "No repositories found for the project."
NO_DEV_STEPS_FOUND_MSG = "No Development Steps found for the user story."


class AgentService:
    def __init__(self):
        # Dont need to initialize anything for now, but this allows us to group agent related methods together and easily add more in the future
        pass

    def _create_agent_logger(
        self,
        session,
        user_id,
        project_id,
        title,
        user_story_id,
        agent_name=None,
        agent_id=None,
    ):
        return create_agent_logger(
            session=session,
            user_id=user_id,
            project_id=project_id,
            agent_run_title=title,
            user_story_id=user_story_id,
            agent_name=agent_name,
            agent_id=agent_id,
        )

    def _create_agent_deps(self, project_id, user_id, input_id, session, agent_logger):
        return AgentDeps(
            project_id=project_id,
            user_id=user_id,
            input_id=input_id,
            input_type="user_story",
            session=session,
            agent_logger=agent_logger,
        )

    def create_gherkin(self, session, project_id, user_data, story):
        agent_logger = self._create_agent_logger(
            session,
            user_data.id,
            project_id,
            f"Criação de cenários Gherkin para user story {story.name}",
            story.id,
            agent_name="gherkin",
        )
        deps = self._create_agent_deps(
            project_id, user_data.id, story.id, session, agent_logger
        )

        logger.info("Creating Gherkin scenarios")
        gherkin_orm = gherkin_agent.run(
            deps=deps,
        )
        deps.session.add_all(gherkin_orm)
        deps.session.commit()
        logger.info(
            f"{len(gherkin_orm)} Gherkin rules created for user story {story.name}"
        )

    def create_user_story_dev_steps(self, session, project_id, user_data, story):
        user_story = (
            session.query(UserStory)
            .filter_by(id=story.id)
            .options(joinedload(UserStory.dev_steps))
            .options(
                joinedload(UserStory.gherkin_rules).joinedload(GherkinRule.scenarios)
            )
            .scalar()
        )

        # Check and delete existing dev steps before creating new ones
        if user_story.dev_steps:
            for dev_step in user_story.dev_steps:
                session.delete(dev_step)
        user_story.log = "Iniciando geração de roteiro de desenvolvimento."
        user_story.timestamp = datetime.now(timezone.utc)
        session.commit()
        user_story_public = UserStoryPublic.model_validate(user_story)
        user_message = user_story_public.model_dump_json()

        agent_logger = self._create_agent_logger(
            session,
            user_data.id,
            project_id,
            f"Roteiro de desenvolvimento para User Story - {user_story.name}",
            user_story.id,
            agent_name="user_story_to_dev_steps",
        )

        repositories = (
            session.query(ProjectRepository).filter_by(project_id=project_id).all()
        )
        if not repositories:
            logger.error(NO_REPOS_FOUND_MSG)
            agent_logger.log(NO_REPOS_FOUND_MSG, True)
            return None

        user_story_to_dev_steps_agent = create_agent_from_database(
            project_id=project_id,
            name="user_story_to_dev_steps",
            session=session,
            output_type=list[UserStoryDevStepBase],
            agent_logger=agent_logger,
        )

        deps = self._create_agent_deps(
            project_id, user_data.id, user_story.id, session, agent_logger
        )
        agent_logger.log("Transformando user story em development steps...")

        result = user_story_to_dev_steps_agent.run_sync(
            user_message,
            deps=deps,
            output_type=list[UserStoryDevStepBase],
        )

        dev_steps = result.output
        dev_steps_db = []
        for dev_step in dev_steps:
            dev_steps_db.append(
                UserStoryDevStep(
                    order=dev_step.order,
                    name=dev_step.name,
                    description=dev_step.description,
                    user_story_id=user_story.id,
                )
            )

        session.add_all(dev_steps_db)
        user_story.log = "Roteiro de desenvolvimento gerado."
        session.commit()

        agent_logger.log(f"dev steps gerados: {dev_steps}")
        agent_logger.finish_agent_run_with_usage(
            "Execução finalizada", result.response.model_name, result.usage
        )

    def create_tdd_unit_tests(self, session, project_id, user_data, story):
        """
        Generate TDD-style unit tests for a user story and create a PR via the GitHub MCP server.

        This method:
        - Retrieves all repositories for the given project from the database.
        - Retrieves all Gherkin rules associated with the given user story.
        - Creates an agent run and dependencies.
        - Invokes the unit test agent to generate tests and open a pull request.

        Args:
            session: SQLAlchemy session used to query and persist data.
            project_id: ID of the project whose repositories will be used.
            user_data: Authenticated user object initiating the operation (must have an ``id`` attribute).
            story: User story object for which Gherkin rules will be loaded (must have ``id`` and ``name`` attributes).

        Returns:
            str | None: The URL of the created pull request, or ``None`` if no repositories
            or Gherkin rules are found and the operation cannot proceed.
        """

        # 1. Create agent logger and deps
        agent_logger = self._create_agent_logger(
            session,
            user_data.id,
            project_id,
            f"Geração de testes unitários para User Story - {story.name}",
            story.id,
            agent_name="unit_test_agent",
        )
        deps = self._create_agent_deps(
            project_id, user_data.id, story.id, session, agent_logger
        )

        logger.info(f"Starting TDD unit test generation for story: {story.id}")
        agent_logger.log(f"Iniciando geração dos testes unitários para US: {story.id}")

        # 2. Retrieve necessary data for the agent run
        repositories = (
            session.query(ProjectRepository).filter_by(project_id=project_id).all()
        )
        if not repositories:
            logger.error(NO_REPOS_FOUND_MSG)
            agent_logger.log(NO_REPOS_FOUND_MSG, True)
            return None

        gherkin_rules = (
            session.query(GherkinRule)
            .options(joinedload(GherkinRule.scenarios))
            .filter_by(user_story_id=story.id)
            .all()
        )
        if not gherkin_rules:
            logger.error("No Gherkin rules found for the user story.")
            agent_logger.log("No Gherkin rules found for the user story.", True)
            return None

        repositories_public = [
            ProjectRepositoryPublic.model_validate(repo) for repo in repositories
        ]
        gherkin_rules_public = [
            GherkinRuleFullToAgentRunPublic.model_validate(rule)
            for rule in gherkin_rules
        ]

        return unit_test_agent.run(
            repositories=[repo.model_dump_json() for repo in repositories_public],
            gherkin_rules=[rule.model_dump_json() for rule in gherkin_rules_public],
            deps=deps,
        )

    def create_tdd_code(self, session, project_id, user_data, story):

        agent_logger = self._create_agent_logger(
            session,
            user_data.id,
            project_id,
            f"Geração do código baseado em testes unitários para User Story - {story.name}",
            story.id,
            agent_name="code_agent",
        )
        deps = self._create_agent_deps(
            project_id, user_data.id, story.id, session, agent_logger
        )

        logger.info(f"Starting TDD code generation for story: {story.id}")
        agent_logger.log(
            f"Iniciando geração do código baseado em testes unitários para US: {story.id}"
        )

        repositories = (
            session.query(ProjectRepository).filter_by(project_id=project_id).all()
        )
        if not repositories:
            logger.error(NO_REPOS_FOUND_MSG)
            agent_logger.log(NO_REPOS_FOUND_MSG, True)
            return None

        dev_steps = (
            session.query(UserStoryDevStep).filter_by(user_story_id=story.id).all()
        )
        if not dev_steps:
            logger.error(NO_DEV_STEPS_FOUND_MSG)
            agent_logger.log(NO_DEV_STEPS_FOUND_MSG, True)
            return None

        repositories_public = [
            ProjectRepositoryPublic.model_validate(repo) for repo in repositories
        ]
        dev_steps_public = [
            UserStoryDevStepPublic.model_validate(step) for step in dev_steps
        ]

        return code_agent.run(
            repositories=[repo.model_dump_json() for repo in repositories_public],
            dev_steps=[step.model_dump_json() for step in dev_steps_public],
            deps=deps,
        )

    def run_openhands_task(self, session, project_id, user_data, prompt):
        """Run a generic OpenHands task on all project repositories."""
        agent_logger = create_agent_logger(
            session=session,
            user_id=user_data.id,
            project_id=project_id,
            agent_run_title="Tarefa genérica com OpenHands",
            agent_name="openhands",
        )
        deps = AgentDeps(
            project_id=project_id,
            user_id=user_data.id,
            session=session,
            agent_logger=agent_logger,
        )

        logger.info(f"Starting OpenHands task for project: {project_id}")
        agent_logger.log(f"Iniciando tarefa com OpenHands no projeto: {project_id}")

        repositories = (
            session.query(ProjectRepository).filter_by(project_id=project_id).all()
        )
        if not repositories:
            logger.error(NO_REPOS_FOUND_MSG)
            agent_logger.log(NO_REPOS_FOUND_MSG, True)
            return None

        return openhands_agent.run(
            prompt=prompt,
            repositories=[
                OpenHandsRepository.model_validate(repo) for repo in repositories
            ],
            deps=deps,
        )


agent_service = AgentService()


def create_user_story_dev_steps_in_background(
    project_id: str,
    user_id: str,
    story_id: str,
):
    with create_session() as session:
        story = session.query(UserStory).filter_by(id=story_id).first()
        user_data = session.query(User).filter_by(id=user_id).first()
        if story and user_data:
            agent_service.create_user_story_dev_steps(
                session=session,
                project_id=project_id,
                user_data=user_data,
                story=story,
            )


def run_openhands_task_in_background(project_id: str, user_id: str, prompt: str):
    with create_session() as session:
        user_data = session.query(User).filter_by(id=user_id).first()
        if user_data:
            agent_service.run_openhands_task(
                session=session,
                project_id=project_id,
                user_data=user_data,
                prompt=prompt,
            )
