from typing import Final

import httpx
from sqlalchemy.orm import Session

from database.models import (
    JiraIntegrationCardsMapping,
    JiraIntegrationColumnsMapping,
    JiraIntegrationConfig,
    Project,
    ProjectKanbanColumn,
    UserStory,
)
from integrations.base import IntegrationProvider
from utils import log

logger = log.setup_custom_logger(__name__)

ACCEPT_HEADER: Final[str] = "application/json"

class JiraIntegrationProvider(IntegrationProvider):
    def __init__(self, session: Session):
        self.session = session

    @staticmethod
    def _build_description_doc(description: str | None) -> dict:
        return {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": description or ""}],
                }
            ],
        }

    @staticmethod
    def _find_matching_transition_id(
            transitions: list[dict], column_mapping: JiraIntegrationColumnsMapping
    ) -> str | None:
        target_id = str(column_mapping.jira_status_id or "").strip()
        target_name = (column_mapping.jira_status_name or "").strip().lower()

        for t in transitions:
            to_status = t.get("to", {})
            to_id = str(to_status.get("id", "")).strip()
            to_name = (to_status.get("name", "")).strip().lower()
            t_id = str(t.get("id", "")).strip()
            t_name = (t.get("name", "")).strip().lower()

            if (target_id and target_id in (to_id, t_id)) or (
                    target_name and target_name in (to_name, t_name)
            ):
                return t.get("id")
        return None

    async def _get_config(self, project: Project) -> JiraIntegrationConfig:
        config = self.session.query(JiraIntegrationConfig).filter(
            JiraIntegrationConfig.project_id == project.id).first()
        if not config:
            raise ValueError("Jira integration not configured for this project")
        return config

    async def test_connection(self, config: JiraIntegrationConfig) -> bool:
        logger.info(f"Testing Jira connection for config: {config.id}")
        auth = (config.jira_email, config.jira_api_token)
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{config.jira_base_url.rstrip('/')}/rest/api/3/project/{config.jira_project_key}",
                auth=auth,
            )
            resp.raise_for_status()
            logger.info(f"Jira connection test successful for config: {config.id}")
            return True

    async def get_projects(
        self, base_url: str, email: str, token: str
    ) -> list[dict]:
        logger.info(f"Fetching Jira projects from {base_url}")
        auth = (email, token)
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{base_url.rstrip('/')}/rest/api/3/project",
                headers={"Accept": ACCEPT_HEADER},
                auth=auth,
            )
            resp.raise_for_status()
            return resp.json()

    async def get_board_columns(
            self,
            base_url: str,
            email: str,
            token: str,
            board_id: str | int | None = None,
            project_key_or_id: str | None = None,
    ) -> dict:
        logger.info(
            f"Fetching Jira board configuration for board={board_id}, project={project_key_or_id}"
        )
        auth = (email, token)
        async with httpx.AsyncClient() as client:
            target_board_id = await self._resolve_board_id(
                client, base_url, auth, board_id, project_key_or_id
            )

            board_config = await self._fetch_board_config(
                client, base_url, auth, target_board_id
            )

            target_project_key_or_id = self._extract_project_key(
                board_config, project_key_or_id
            )

            default_issue_type = await self._fetch_default_issue_type(
                client, base_url, auth, target_project_key_or_id
            )

            board_config["default_issue_type"] = default_issue_type
            return board_config

    async def _resolve_board_id(
            self,
            client: httpx.AsyncClient,
            base_url: str,
            auth: tuple,
            board_id: str | int | None,
            project_key_or_id: str | None,
    ) -> str | int:
        if board_id:
            return board_id

        if not project_key_or_id:
            raise ValueError(
                f"No agile board found for project '{project_key_or_id}' or board_id was not provided"
            )

        boards_resp = await client.get(
            f"{base_url.rstrip('/')}/rest/agile/1.0/board",
            params={"projectKeyOrId": str(project_key_or_id)},
            headers={"Accept": ACCEPT_HEADER},
            auth=auth,
        )
        boards_resp.raise_for_status()
        boards_data = boards_resp.json().get("values", [])

        if not boards_data:
            raise ValueError(
                f"No agile board found for project '{project_key_or_id}' or board_id was not provided"
            )

        return boards_data[0].get("id")

    async def _fetch_board_config(
            self,
            client: httpx.AsyncClient,
            base_url: str,
            auth: tuple,
            board_id: str | int,
    ) -> dict:
        config_resp = await client.get(
            f"{base_url.rstrip('/')}/rest/agile/1.0/board/{board_id}/configuration",
            headers={"Accept": ACCEPT_HEADER},
            auth=auth,
        )
        config_resp.raise_for_status()
        return config_resp.json()

    def _extract_project_key(
            self, board_config: dict, project_key_or_id: str | None
    ) -> str | None:
        if project_key_or_id:
            return project_key_or_id

        loc = board_config.get("location", {})
        return (
                loc.get("projectKeyOrId")
                or loc.get("projectKey")
                or loc.get("projectId")
                or loc.get("key")
                or loc.get("id")
        )

    async def _fetch_default_issue_type(
            self,
            client: httpx.AsyncClient,
            base_url: str,
            auth: tuple,
            project_key_or_id: str | None,
    ) -> str | None:
        if not project_key_or_id:
            return None

        try:
            project_resp = await client.get(
                f"{base_url.rstrip('/')}/rest/api/3/project/{project_key_or_id}",
                headers={"Accept": ACCEPT_HEADER},
                auth=auth,
            )
            if project_resp.status_code != 200:
                return None

            project_data = project_resp.json()
            issue_types = project_data.get("issueTypes", [])

            return self._extract_default_issue_type(issue_types)
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            logger.warning(
                f"Could not fetch default issue type for project {project_key_or_id}: {exc}"
            )
            return None

    def _extract_default_issue_type(self, issue_types: list) -> str | None:
        if not issue_types or not isinstance(issue_types, list):
            return None
    
        for itype in issue_types:
            if isinstance(itype, dict) and (
                itype.get("isDefault") or itype.get("default")
            ):
                return itype.get("name")
    
        first_itype = issue_types[0]
        return (
            first_itype.get("name")
            if isinstance(first_itype, dict)
            else str(first_itype)
        )

    async def create_issue(self, project: Project, story: UserStory) -> str:
        logger.info(f"Creating Jira issue for user story: {story.id}")
        config = await self._get_config(project)
        auth = (config.jira_email, config.jira_api_token)

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{config.jira_base_url}/rest/api/3/issue",
                auth=auth,
                json={
                    "fields": {
                        "project": {"key": config.jira_project_key},
                        "summary": story.name,
                        "description": self._build_description_doc(story.description),
                        "issuetype": {"name": config.default_issue_type}
                    }
                }
            )
            response.raise_for_status()
            issue_data = response.json()
            return issue_data["key"]

    async def update_issue(self, project: Project, story: UserStory) -> None:
        logger.info(f"Updating Jira issue for user story: {story.id}")
        mapping = self.session.query(JiraIntegrationCardsMapping).filter(
            JiraIntegrationCardsMapping.user_story_id == story.id).first()
        if not mapping:
            raise ValueError("Jira issue mapping not found for this user story")

        config = await self._get_config(project)
        auth = (config.jira_email, config.jira_api_token)

        async with httpx.AsyncClient() as client:
            await client.put(
                f"{config.jira_base_url}/rest/api/3/issue/{mapping.jira_issue_key}",
                auth=auth,
                json={
                    "fields": {
                        "summary": story.name,
                        "description": self._build_description_doc(story.description)
                    }
                }
            )

    async def delete_issue(self, project: Project, story: UserStory) -> None:
        logger.info(f"Deleting Jira issue for user story: {story.id}")
        mapping = self.session.query(JiraIntegrationCardsMapping).filter(
            JiraIntegrationCardsMapping.user_story_id == story.id).first()
        if not mapping:
            logger.warning(f"No Jira issue mapping found for user story {story.id}")
            return

        config = await self._get_config(project)
        auth = (config.jira_email, config.jira_api_token)

        async with httpx.AsyncClient() as client:
            response = await client.delete(
                f"{config.jira_base_url}/rest/api/3/issue/{mapping.jira_issue_key}",
                auth=auth
            )
            response.raise_for_status()
            logger.info(f"Successfully deleted Jira issue {mapping.jira_issue_key}")

            # Clean up the mapping after successful deletion
            self.session.delete(mapping)
            self.session.commit()

    async def update_issue_column(
            self, project: Project, story: UserStory, kanban_column: ProjectKanbanColumn
    ) -> None:
        logger.info(
            f"Updating Jira issue column for user story: {story.id} to column: {kanban_column.id}"
        )
        mapping = (
            self.session.query(JiraIntegrationCardsMapping)
            .filter(JiraIntegrationCardsMapping.user_story_id == story.id)
            .first()
        )
        if not mapping:
            raise ValueError("Jira issue mapping not found for this user story")

        config = await self._get_config(project)
        auth = (config.jira_email, config.jira_api_token)

        column_mapping = (
            self.session.query(JiraIntegrationColumnsMapping)
            .filter(
                JiraIntegrationColumnsMapping.jira_integration_config_id == config.id,
                JiraIntegrationColumnsMapping.project_kanban_column_id == kanban_column.id,
            )
            .first()
        )
        if not column_mapping:
            logger.warning(
                f"No Jira column mapping found for kanban column {kanban_column.id} in project {project.id}"
            )
            return

        async with httpx.AsyncClient() as client:
            transitions_url = (
                f"{config.jira_base_url}/rest/api/3/issue/{mapping.jira_issue_key}/transitions"
            )
            resp = await client.get(transitions_url, auth=auth)
            resp.raise_for_status()
            transitions = resp.json().get("transitions", [])

            target_transition_id = self._find_matching_transition_id(
                transitions, column_mapping
            )
            if not target_transition_id:
                logger.warning(
                    f"No transition found for Jira issue {mapping.jira_issue_key} to status {column_mapping.jira_status_name} ({column_mapping.jira_status_id})"
                )
                return

            transition_resp = await client.post(
                transitions_url,
                auth=auth,
                json={"transition": {"id": target_transition_id}},
            )
            transition_resp.raise_for_status()
            logger.info(
                f"Successfully transitioned Jira issue {mapping.jira_issue_key} to status {column_mapping.jira_status_name}"
            )
