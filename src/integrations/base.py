from abc import ABC, abstractmethod
from typing import Any

from database.models import Project, ProjectKanbanColumn, UserStory


class IntegrationProvider(ABC):
    @abstractmethod
    async def create_issue(self, project: Project, story: UserStory) -> str: ...

    @abstractmethod
    async def delete_issue(self, project: Project, story: UserStory) -> None: ...

    @abstractmethod
    async def update_issue(self, project: Project, story: UserStory) -> None: ...

    @abstractmethod
    async def update_issue_column(self, project: Project, story: UserStory,
                                  kanban_column: ProjectKanbanColumn) -> None: ...

    @abstractmethod
    async def test_connection(self, config: object) -> bool:
        return True

    @abstractmethod
    async def get_projects(self, base_url: str, email: str, token: str) -> list[dict[str, Any]]:
        return []

    @abstractmethod
    async def get_board_columns(
            self,
            base_url: str,
            email: str,
            token: str,
            board_id: str | int | None = None,
            project_key_or_id: str | None = None,
    ) -> dict[str, Any]:
        return {}
