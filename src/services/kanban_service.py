import asyncio

import nest_asyncio

from agents.story_completion_agent import run_story_completion
from database.database import create_session
from database.models import (
    JiraIntegrationCardsMapping,
    Project,
    ProjectKanbanColumn,
    ProjectKanbanColumnRule,
    User,
    UserStory,
)
from integrations.jira_provider import JiraIntegrationProvider
from services.agent_service import agent_service
from services.notification_service import notification_service
from utils import log
from utils.validation import (
    validate_project_kanban_column_in_project,
)

logger = log.setup_custom_logger(__name__)


class KanbanService:

    def __init__(self):
        self.handlers = {
            "create_gherkin": agent_service.create_gherkin,
            "create_tdd_unit_tests": agent_service.create_tdd_unit_tests,
            "create_user_story_dev_steps": agent_service.create_user_story_dev_steps,
            "create_tdd_code": agent_service.create_tdd_code,
        }

    def run_kanban_update_flow(
        self,
        session,
        background_tasks,
        project_id,
        story,
        project_kanban_column_id,
        user_data,
    ):
        new_project_kanban_column = validate_project_kanban_column_in_project(
            session, project_id, project_kanban_column_id
        )

        returning_user_story_to_old_column = (
            new_project_kanban_column.position < story.kanban_column.position
        )

        self.update_kanban_column_and_its_log(session, story, new_project_kanban_column)
        self.update_issue_column_on_jira(session, story, new_project_kanban_column)
        self.mark_done_if_last_column(
            session, project_id, story, background_tasks, user_data
        )

        # Notify all users in the project about the kanban update
        notification_service.notify_project_members(
            session=session,
            project_id=project_id,
            message={
                "storyId": story.id,
                "newColumnId": new_project_kanban_column.id,
            },
            exclude_user_id=story.user_id,
            notification_type="KANBAN_UPDATE",
        )

        if returning_user_story_to_old_column:
            logger.info(
                "User story is returning to an old kanban column, skipping kanban column rules execution"
            )
            return

        self.run_kanban_columns_rules(session, background_tasks, story, user_data)

    def update_kanban_column_and_its_log(
        self, session, story, new_project_kanban_column
    ):
        logger.info(
            f"Updating kanban column for user story {story.id} to column {new_project_kanban_column.id} and updating log accordingly"
        )

        old_project_kanban_column = session.get(
            ProjectKanbanColumn, story.project_kanban_column_id
        )

        story.kanban_column = new_project_kanban_column

        if old_project_kanban_column:
            story.log = f"História atualizada por usuário. Status alterado de '{old_project_kanban_column.name}' para '{new_project_kanban_column.name}' "

    def mark_done_if_last_column(
        self, session, project_id, story, background_tasks, user_data
    ):
        logger.info(
            "Checking if the new kanban column is the last one to potentially mark the story as done"
        )

        project_kanban_columns_count = (
            session.query(ProjectKanbanColumn)
            .filter(ProjectKanbanColumn.project_id == project_id)
            .count()
        )

        logger.info(
            f"Project has {project_kanban_columns_count} kanban columns. Story is in column position {story.kanban_column.position + 1}"
        )

        if story.kanban_column.position == project_kanban_columns_count - 1:
            story.is_done = True
            story.log += " e marcada como concluída."
            background_tasks.add_task(
                run_story_completion,
                project_id,
                story.id,
                user_data.id,
            )

    def run_kanban_columns_rules(self, session, background_tasks, story, user_data):
        logger.info(
            f"Running kanban column rules for kanban column with id {story.kanban_column.id}"
        )

        project_kanban_rule_list = (
            session.query(ProjectKanbanColumnRule)
            .filter(
                ProjectKanbanColumnRule.target_kanban_column_id
                == story.kanban_column.id
            )
            .order_by(ProjectKanbanColumnRule.order.asc())
            .all()
        )

        if not project_kanban_rule_list:
            logger.info(
                f"No kanban column rules found for kanban column with id {story.kanban_column.id}"
            )
            return

        for rule in project_kanban_rule_list:
            logger.info(
                f"Running rule with id {rule.id}, handler_key: {rule.handler_key} and type {rule.action_type}"
            )

            match rule.action_type.lower():
                case "trigger":
                    handler = self.handlers.get(rule.handler_key)

                    if not handler:
                        logger.error(f"No handler found for action {rule.handler_key}")
                        continue

                    story.log += f" iniciada a execução do agent {rule.handler_key}."

                    if rule.is_blocking:
                        logger.info(
                            f"Executing blocking handler {rule.handler_key} for rule with id {rule.id}"
                        )
                        handler(
                            session,
                            story.kanban_column.project_id,
                            user_data,
                            story,
                        )
                    else:
                        logger.info(
                            f"Scheduling non-blocking handler {rule.handler_key} for rule with id {rule.id}"
                        )
                        background_tasks.add_task(
                            _run_handler_in_background,
                            handler,
                            story.kanban_column.project_id,
                            user_data.id,
                            story.id,
                        )
                case "validation":
                    logger.error("Validation rules are not implemented yet")
                    return
                case _:
                    logger.error(
                        f"Unknown rule action type {rule.action_type} for rule with id {rule.id}"
                    )
                    return

    def get_handler_keys(self) -> list[str]:
        return list(self.handlers.keys())

    def update_issue_column_on_jira(self, session, story, new_project_kanban_column):
        try:
            project_id = getattr(story, "project_id", None) or (
                new_project_kanban_column.project_id
                if new_project_kanban_column
                else None
            )
            if not project_id:
                logger.warning(
                    "Project ID not found for story, skipping Jira column update"
                )
                return

            mapping = (
                session.query(JiraIntegrationCardsMapping)
                .filter(JiraIntegrationCardsMapping.user_story_id == story.id)
                .first()
            )
            if not mapping:
                logger.warning(
                    f"Jira integration mapping not found for story {story.id}, skipping Jira column update"
                )
                return

            project = session.query(Project).filter(Project.id == project_id).first()
            if not project:
                logger.warning(
                    f"Project {project_id} not found, skipping Jira column update"
                )
                return

            provider = JiraIntegrationProvider(session)

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(
                    provider.update_issue_column(
                        project, story, new_project_kanban_column
                    )
                )
                return

            if loop.is_running():
                nest_asyncio.apply()
                asyncio.run(
                    provider.update_issue_column(
                        project, story, new_project_kanban_column
                    )
                )
            else:
                loop.run_until_complete(
                    provider.update_issue_column(
                        project, story, new_project_kanban_column
                    )
                )
        except Exception:
            logger.exception(
                f"Error updating Jira issue column for story {getattr(story, 'id', None)}"
            )


def _run_handler_in_background(handler, project_id: str, user_data_id: str, story_id: str):
    with create_session() as session:
        story = session.query(UserStory).filter_by(id=story_id).first()
        user_data = session.query(User).filter_by(id=user_data_id).first()
        if story and user_data:
            handler(session, project_id, user_data, story)


kanban_service = KanbanService()
