from typing import Iterable, TypeVar

from sqlalchemy import case, func, literal
from sqlalchemy.sql.elements import ColumnElement

from database import models

UNKNOWN_MODEL_LABEL = "Não informado"
SPEC_WITHOUT_STORY_LABEL = "Execuções sem User Story vinculada"
ORPHAN_SPEC_GROUP_ID = "__content_analysis__"

ModelUsageT = TypeVar("ModelUsageT")
RankedItemT = TypeVar("RankedItemT")


def model_label_column() -> ColumnElement:
    return func.coalesce(
        func.nullif(models.AgentRun.model, ""),
        UNKNOWN_MODEL_LABEL,
    )


def token_sum_column() -> ColumnElement:
    return (
        func.coalesce(func.sum(models.AgentRun.input_tokens), 0)
        + func.coalesce(func.sum(models.AgentRun.output_tokens), 0)
    )


def specification_group_columns() -> tuple[ColumnElement, ColumnElement]:
    spec_id = case(
        (models.AgentRun.user_story_id.isnot(None), models.UserStory.id),
        else_=literal(ORPHAN_SPEC_GROUP_ID),
    )
    spec_label = case(
        (models.AgentRun.user_story_id.isnot(None), models.UserStory.name),
        else_=literal(SPEC_WITHOUT_STORY_LABEL),
    )
    return spec_id, spec_label


def build_ranked_items(
    rows: Iterable[tuple],
    model_usage_class: type[ModelUsageT],
    ranked_item_class: type[RankedItemT],
) -> list[RankedItemT]:
    by_entity: dict[str, dict[str, object]] = {}

    for row in rows:
        if len(row) == 7:
            entity_id, project_id, label, model, agent_id, agent_name, total_tokens = row
        elif len(row) == 6:
            entity_id, project_id, label, model, agent_id, total_tokens = row
            agent_name = None
        else:
            entity_id, project_id, label, model, total_tokens = row
            agent_id = None
            agent_name = None

        tokens = int(total_tokens or 0)
        if tokens <= 0:
            continue

        bucket = by_entity.setdefault(
            entity_id,
            {"project_id": project_id, "label": label, "models": {}},
        )
        bucket["label"] = label
        bucket["project_id"] = project_id
        models_map: dict[tuple[str, str | None, str | None], int] = bucket["models"]  # type: ignore[assignment]
        key = (model, agent_id, agent_name)
        models_map[key] = models_map.get(key, 0) + tokens

    response: list[RankedItemT] = []
    for entity_id, data in by_entity.items():
        models_map = data["models"]  # type: ignore[assignment]
        models_usage = [
            model_usage_class(
                model=key[0],
                agent_id=key[1],
                agent_name=key[2],
                total_tokens=tokens,
            )
            for key, tokens in models_map.items()
            if tokens > 0
        ]
        models_usage.sort(key=lambda item: item.total_tokens, reverse=True)  # type: ignore[attr-defined]
        total = sum(item.total_tokens for item in models_usage)  # type: ignore[attr-defined]
        if total <= 0:
            continue

        response.append(
            ranked_item_class(
                id=entity_id,
                project_id=str(data["project_id"]),
                label=str(data["label"]),
                total_tokens=total,
                models=models_usage,
            )
        )

    response.sort(key=lambda item: item.total_tokens, reverse=True)  # type: ignore[attr-defined]
    return response
