import asyncio

import nest_asyncio
from sqlalchemy.orm import Session

from database.models import (
    JiraIntegrationCardsMapping,
    JiraIntegrationColumnsMapping,
    JiraIntegrationConfig,
    Project,
    User,
    UserStory,
)
from integrations.jira_provider import JiraIntegrationProvider
from utils import log

logger = log.setup_custom_logger(__name__)


def _run_coroutine(coro):
    if not asyncio.iscoroutine(coro):
        return coro
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    if loop.is_running():
        nest_asyncio.apply()
        return asyncio.run(coro)
    else:
        return loop.run_until_complete(coro)


def _parse_jira_description(description_field) -> str:
    """
    Convert Jira description to string format.

    Handles both ADF (Atlassian Document Format) and plain string formats.

    Args:
        description_field: The description field from Jira, can be dict (ADF) or string

    Returns:
        String representation of the description
    """
    if isinstance(description_field, dict):
        content = description_field.get("content") or []
        if content and isinstance(content[0], dict):
            inner_content = content[0].get("content") or []
            if inner_content and isinstance(inner_content[0], dict):
                return inner_content[0].get("text", "")
        return ""
    return str(description_field) if description_field else ""


def _update_story_resolution(story, fields):
    resolution = fields.get("resolution", None)
    story.is_done = resolution is not None


class SyncService:
    def __init__(self, session: Session):
        self.session = session
        self.providers = [JiraIntegrationProvider(session)]

    def sync_create(self, project_id: str, story_id: str):
        logger.info(f"Syncing creation for project {project_id}, story {story_id}")
        project = self.session.query(Project).filter(Project.id == project_id).first()
        story = self.session.query(UserStory).filter(UserStory.id == story_id).first()
        if not project or not story:
            return
        jira_integration_config = (
            self.session.query(JiraIntegrationConfig)
            .filter(JiraIntegrationConfig.project_id == project.id)
            .first()
        )
        if not jira_integration_config:
            return
        for provider in self.providers:
            try:
                jira_issue_key = _run_coroutine(provider.create_issue(project, story))
                logger.info(f"Jira issue created successfully: {jira_issue_key}")

                existing_mapping = (
                    self.session.query(JiraIntegrationCardsMapping)
                    .filter(JiraIntegrationCardsMapping.user_story_id == str(story.id))
                    .first()
                )
                if not existing_mapping:
                    mapping = JiraIntegrationCardsMapping(
                        user_story_id=str(story.id),
                        jira_issue_key=jira_issue_key,
                        jira_integration_config_id=str(jira_integration_config.id),
                    )
                    self.session.add(mapping)
                    self.session.commit()
                    logger.info(f"Jira integration mapping saved for story {story.id}")
                else:
                    logger.info(f"Updating existing mapping for story {story.id}")
                    _run_coroutine(provider.update_issue(project, story))
            except Exception as e:  # noqa: BLE001
                logger.error(f"Error creating issue: {e}")

    async def sync_update(self, project_id: str, story_id: str):
        logger.info(f"Syncing update for project {project_id}, story {story_id}")
        project = self.session.query(Project).filter(Project.id == project_id).first()
        story = self.session.query(UserStory).filter(UserStory.id == story_id).first()
        if project and story:
            for provider in self.providers:
                try:
                    await provider.update_issue(project, story)
                except Exception as e:  # noqa: BLE001
                    logger.error(f"Error updating issue: {e}")

    async def sync_delete(self, project_id: str, story_id: str):
        logger.info(f"Syncing delete for project {project_id}, story {story_id}")
        project = self.session.query(Project).filter(Project.id == project_id).first()
        story = self.session.query(UserStory).filter(UserStory.id == story_id).first()
        if not project or not story:
            return
        for provider in self.providers:
            try:
                await provider.delete_issue(project, story)
            except Exception as e:  # noqa: BLE001
                logger.error(f"Error deleting issue: {e}")

    def handle_webhook(self, payload: dict):
        try:
            logger.info(f"Handling incoming Jira webhook. Payload: {payload}")

            webhook_event = payload.get("webhookEvent", None)
            if webhook_event is None:
                logger.warning("Webhook received has no event")
                return

            issue = payload.get("issue", {})
            key = issue.get("key")
            if not key:
                logger.warning("Webhook received without issue key")
                return

            if webhook_event == "jira:issue_created":
                self._create_user_story_from_jira_issue(issue, key)
            elif webhook_event == "jira:issue_updated":
                self._update_user_story_from_jira_issue(issue, key)
            elif webhook_event == "jira:issue_deleted":
                self._delete_user_story_from_jira_issue(key)
        except Exception:
            logger.exception("Error handling webhook")
            raise

    def _create_user_story_from_jira_issue(self, issue, key):
        """
        Create a new UserStory from a Jira issue webhook payload.

        Args:
            issue: The issue object from the webhook payload
            key: The Jira issue key

        Returns:
            The newly created UserStory, or None if creation failed
        """
        # Extract issue details from webhook payload
        fields = issue.get("fields", {})

        # Example: Extract project ID from Jira issue
        project = fields.get("project", "")
        jira_project_id = project.get("id", "")
        jira_project_key = project.get("key", "")
        jira_integration_config = (
            self.session.query(JiraIntegrationConfig)
            .filter(JiraIntegrationConfig.jira_project_id == jira_project_id)
            .filter(JiraIntegrationConfig.jira_project_key == jira_project_key)
            .first()
        )

        if not jira_integration_config:
            logger.warning(
                f"No JiraIntegrationConfig found for project ID: {jira_project_id}"
            )
            raise ValueError(
                f"No JiraIntegrationConfig found for project ID: {jira_project_id}"
            )

        jira_integration_card_mapping = (
            self.session.query(JiraIntegrationCardsMapping)
            .filter(JiraIntegrationCardsMapping.jira_issue_key == key)
            .first()
        )
        if jira_integration_card_mapping:
            logger.info(f"JiraIntegrationCardsMapping found for key: {key}")
            return None

        summary = fields.get("summary", "")
        description = _parse_jira_description(fields.get("description", ""))

        # Extract story points (may vary based on custom field configuration)
        story_points = fields.get("customfield_10016", 0)  # Adjust field ID as needed
        if story_points is None:
            story_points = 0

        user = (
            self.session.query(User)
            .filter(User.email == "e-stack@elumini-it.com")
            .first()
        )
        if user is None:
            logger.error("User with email 'e-stack@elumini-it.com' not found")
            raise ValueError("User with email 'e-stack@elumini-it.com' not found")
        new_story = UserStory(
            name=summary,
            description=description,
            points=int(story_points),
            is_done=False,
            log="Created from Jira webhook",
            project_id=str(jira_integration_config.project_id),
            user_id=str(user.id),
        )

        self.session.add(new_story)
        self.session.flush()

        # Create mapping
        mapping = JiraIntegrationCardsMapping(
            user_story_id=str(new_story.id),
            jira_issue_key=key,
            jira_integration_config_id=str(jira_integration_config.id),
        )
        self.session.add(mapping)
        self.session.commit()

        logger.info(f"Created new UserStory {new_story.id} from Jira issue {key}")
        return new_story

    def _update_user_story_from_jira_issue(self, issue, key):
        logger.info("Handling an update event from Jira Webhook")
        jira_integration_card_mapping = (
            self.session.query(JiraIntegrationCardsMapping)
            .filter(JiraIntegrationCardsMapping.jira_issue_key == key)
            .first()
        )

        if not jira_integration_card_mapping:
            logger.info("User story not updated because it was not found on DB")
            return

        story = (
            self.session.query(UserStory)
            .filter(UserStory.id == jira_integration_card_mapping.user_story_id)
            .first()
        )
        if not story:
            logger.error(
                f"Story {jira_integration_card_mapping.user_story_id} not found for Jira issue {key}"
            )
            raise ValueError(
                f"Story {jira_integration_card_mapping.user_story_id} not found for Jira issue {key}"
            )

        logger.info(f"Updating story {story.id} from Jira issue {key}")
        fields = issue.get("fields", {})

        _update_story_resolution(story, fields)
        self._update_story_status(
            story, fields, jira_integration_card_mapping.jira_integration_config_id
        )

        story.log = "Updated from Jira webhook"
        story.name = fields.get("summary", "")
        story.description = _parse_jira_description(fields.get("description", ""))

        self.session.add(story)
        self.session.commit()

        logger.info(f"UserStory {story.id} updated from Jira webhook event")

    def _update_story_status(self, story, fields, jira_integration_config_id):
        status = fields.get("status", None)
        if not status:
            return

        jira_status_id = status.get("id", None)
        jira_integration_column_mapping = (
            self.session.query(JiraIntegrationColumnsMapping)
            .filter(
                JiraIntegrationColumnsMapping.jira_status_id == jira_status_id,
                JiraIntegrationColumnsMapping.jira_integration_config_id
                == jira_integration_config_id,
            )
            .first()
        )
        if jira_integration_column_mapping:
            story.project_kanban_column_id = (
                jira_integration_column_mapping.project_kanban_column_id
            )

    def _delete_user_story_from_jira_issue(self, key):
        logger.info("Handling a delete event from Jira Webhook")
        mapping = (
            self.session.query(JiraIntegrationCardsMapping)
            .filter(JiraIntegrationCardsMapping.jira_issue_key == key)
            .first()
        )
        if mapping:
            story = (
                self.session.query(UserStory)
                .filter(UserStory.id == mapping.user_story_id)
                .first()
            )
            if story:
                self.session.delete(story)
                self.session.commit()
                logger.info(f"UserStory {story.id} deleted from Jira webhook event")
            else:
                logger.error("User story not found for deletion")
                raise ValueError("User story not found for deletion")
