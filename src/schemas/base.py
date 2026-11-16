import datetime
import json

from fastapi.params import Query
from pydantic import BaseModel, ConfigDict, Field


def remove_id_fields(d):
    if isinstance(d, dict):
        # Create a list of keys to avoid changing dict size during iteration
        keys = list(d.keys())
        for key in keys:
            if key == "id" or key.endswith("_id"):
                del d[key]
            else:
                remove_id_fields(d[key])
    elif isinstance(d, list):
        for item in d:
            remove_id_fields(item)


class Base(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    def format_output(self, remove_ids: bool = False) -> str:
        data = self.model_dump()

        """Formats the output of the model for better readability."""
        if remove_ids:
            remove_id_fields(data)
        return json.dumps(data, indent=2, ensure_ascii=False, default=str)


def date_now():
    return datetime.datetime.now(datetime.timezone.utc)


class Pagination(Base):
    page: int = Field(1, ge=1, description="The page number (starting from 1)")
    size: int = Field(10, ge=1, le=100, description="The number of items per page")


def pagination_params(
    page: int = Query(ge=1, default=1, required=False, le=1000),  # type: ignore
    size: int = Query(ge=1, le=100, default=10, required=False),  # type: ignore
):
    return Pagination(page=page, size=size)


class ProjectFullQueryParams(Base):
    user_story_name: str | None = None
    user_story_description: str | None = None
    user_story_project_kanban_columns: list[str] | None = None


def project_full_query_params(
    user_story_name: str | None = Query(default=None, required=False),
    user_story_description: str | None = Query(default=None, required=False),
    user_story_project_kanban_columns: list[str] | None = Query(
        default=None, required=False
    ),
):
    return ProjectFullQueryParams(
        user_story_name=user_story_name,
        user_story_description=user_story_description,
        user_story_project_kanban_columns=user_story_project_kanban_columns,
    )
