from typing import List, Annotated

from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import selectinload

import utils.log as log
from database.database import SessionDep, delete_entity
from database.models import ProjectKanbanColumn, ProjectKanbanColumnRule
from schemas.user_story_schemas import (
    ProjectKanbanColumnFullPublic,
    ProjectKanbanColumnPublic,
    ProjectKanbanColumnWithUserStoriesPublic, ProjectKanbanColumnReorder, ProjectKanbanColumnCreate,
    ProjectKanbanColumnCreateResponse, ProjectKanbanColumnUpdateResponse, ProjectKanbanColumnUpdate,
)
from utils.bearer_auth import check_project_access

router = APIRouter()
logger = log.setup_custom_logger(__name__)


@router.get(
    "/{project_id}",
    response_model=List[ProjectKanbanColumnWithUserStoriesPublic],
)
def get_project_kanban(session: SessionDep, project_id: str,
                       has_access: Annotated[bool, Depends(check_project_access)]):
    logger.info(
        f"Listing ProjectKanbanColumns with user stories for project_id: {project_id}"
    )
    project_kanban = (
        session.query(ProjectKanbanColumn)
        .filter(ProjectKanbanColumn.project_id == project_id)
        .order_by(ProjectKanbanColumn.position.asc())
        .options(
            selectinload(ProjectKanbanColumn.user_stories),
        )
        .all()
    )
    return [
        ProjectKanbanColumnWithUserStoriesPublic.model_validate(context)
        for context in project_kanban
    ]


@router.get("/{project_id}/columns",
            response_model=List[ProjectKanbanColumnPublic],
            response_model_exclude={
                "__all__": {
                    "rules": {
                        "__all__": {
                            "target_kanban_column_id",
                            "payload_schema",
                        }
                    }
                }
            })
def get_project_kanban(session: SessionDep, project_id: str,
                       has_access: Annotated[bool, Depends(check_project_access)]):
    logger.info(f"Listing ProjectKanbanColumns for project_id: {project_id}")
    project_kanban = (
        session.query(ProjectKanbanColumn)
        .filter(ProjectKanbanColumn.project_id == project_id)
        .options(selectinload(ProjectKanbanColumn.rules))
        .order_by(ProjectKanbanColumn.position.asc())
        .all()
    )
    return [
        ProjectKanbanColumnPublic.model_validate(context) for context in project_kanban
    ]


@router.get(
    "/{project_id}/columns/{column_id}",
    response_model=ProjectKanbanColumnFullPublic,
    responses={404: {"description": "Kanban column not found"}},
)
def get_project_kanban_column_by_id(
        session: SessionDep,
        project_id: str,
        column_id: str,
        has_access: Annotated[bool, Depends(check_project_access)]
):
    logger.info(
        f"Received request to get full info for kanban column with id: {column_id} from project_id: {project_id}"
    )

    column = (
        session.query(ProjectKanbanColumn)
        .filter(
            ProjectKanbanColumn.id == column_id,
            ProjectKanbanColumn.project_id == project_id,
        )
        .options(selectinload(ProjectKanbanColumn.rules))
        .first()
    )

    if not column:
        raise HTTPException(
            status_code=404,
            detail=f"Kanban column with id '{column_id}' not found in project '{project_id}'",
        )

    return ProjectKanbanColumnFullPublic.model_validate(column)


@router.put(
    "/{project_id}/columns/reorder",
    response_model=List[ProjectKanbanColumnPublic],
    responses={404: {"description": "Kanban column not found"}},
)
def reorder_project_kanban_columns(
        session: SessionDep,
        project_id: str,
        columns: List[ProjectKanbanColumnReorder],
        has_access: Annotated[bool, Depends(check_project_access)]
):
    logger.info(
        f"Received request to reorder kanban columns for project_id: {project_id}"
    )

    updated_columns = []
    for column_update in columns:
        column = (
            session.query(ProjectKanbanColumn)
            .filter(
                ProjectKanbanColumn.id == column_update.id,
                ProjectKanbanColumn.project_id == project_id,
            )
            .first()
        )

        if not column:
            raise HTTPException(
                status_code=404,
                detail=f"Kanban column with id '{column_update.id}' not found in project '{project_id}'",
            )

        column.position = column_update.position
        updated_columns.append(column)

    session.commit()
    for column in updated_columns:
        session.refresh(column)

    return [ProjectKanbanColumnPublic.model_validate(c) for c in updated_columns]


@router.post(
    "/{project_id}/columns",
    response_model=ProjectKanbanColumnCreateResponse,
    status_code=201,
)
def create_project_kanban_column(
        session: SessionDep,
        project_id: str,
        column: ProjectKanbanColumnCreate,
        has_access: Annotated[bool, Depends(check_project_access)]
):
    logger.info(
        f"Received request to create a kanban column for project_id: {project_id}"
    )

    column_count = (
        session.query(ProjectKanbanColumn)
        .filter(
            ProjectKanbanColumn.project_id == project_id,
        )
        .count()
    )

    new_column = ProjectKanbanColumn(
        name=column.name,
        position=column_count,
        column_change_warning=column.column_change_warning,
        project_id=project_id,
    )
    session.add(new_column)
    session.commit()
    session.refresh(new_column)

    for rule in column.rules:
        new_column_rule = ProjectKanbanColumnRule(
            action_type=rule.action_type,
            provider=rule.provider,
            handler_key=rule.handler_key,
            error_message=rule.error_message,
            payload_schema=rule.payload_schema,
            is_blocking=rule.is_blocking,
            order=rule.order,
            target_kanban_column_id=new_column.id,
        )
        session.add(new_column_rule)
        session.commit()
        session.refresh(new_column_rule)

    return new_column


@router.put(
    "/{project_id}/columns/{column_id}",
    response_model=ProjectKanbanColumnUpdateResponse,
    responses={404: {"description": "Kanban column not found"}},
)
def update_project_kanban_column(
        session: SessionDep,
        project_id: str,
        column_id: str,
        column_update: ProjectKanbanColumnUpdate,
        has_access: Annotated[bool, Depends(check_project_access)]
):
    logger.info(
        f"Received request to update kanban column with id: {column_id} from project_id: {project_id}"
    )

    column = (
        session.query(ProjectKanbanColumn)
        .filter(
            ProjectKanbanColumn.id == column_id,
            ProjectKanbanColumn.project_id == project_id,
        )
        .first()
    )

    if not column:
        raise HTTPException(
            status_code=404,
            detail=f"Kanban column with id '{column_id}' not found in project '{project_id}'",
        )

    if column_update.name is not None:
        column.name = column_update.name
    if column_update.column_change_warning is not None:
        column.column_change_warning = column_update.column_change_warning

    if column_update.rules is not None:
        # Clear existing rules
        for rule in column.rules:
            session.delete(rule)
        session.flush()

        # Add new rules
        for rule in column_update.rules:
            new_column_rule = ProjectKanbanColumnRule(
                action_type=rule.action_type,
                provider=rule.provider,
                handler_key=rule.handler_key,
                error_message=rule.error_message,
                payload_schema=rule.payload_schema,
                is_blocking=rule.is_blocking,
                order=rule.order,
                target_kanban_column_id=column.id,
            )
            session.add(new_column_rule)

    session.commit()
    session.refresh(column)
    return column


@router.delete(
    "/{project_id}/columns/{column_id}",
    response_model=dict,
    responses={
        200: {"description": "Kanban column deleted successfully"},
        404: {"description": "Kanban column not found"},
        400: {"description": "Kanban column cannot be deleted because it has associated user stories"}
    },
)
def delete_project_kanban_column(
        session: SessionDep,
        project_id: str,
        column_id: str,
        has_access: Annotated[bool, Depends(check_project_access)]
):
    logger.info(
        f"Received request to delete kanban column with id: {column_id} from project_id: {project_id}"
    )

    column = (
        session.query(ProjectKanbanColumn)
        .filter(
            ProjectKanbanColumn.id == column_id,
            ProjectKanbanColumn.project_id == project_id,
        )
        .options(
            selectinload(ProjectKanbanColumn.user_stories),
        )
        .first()
    )

    if not column:
        raise HTTPException(
            status_code=404,
            detail=f"Kanban column with id '{column_id}' not found in project '{project_id}'",
        )
    elif column.user_stories:
        raise HTTPException(
            status_code=400,
            detail=f"Kanban column with id '{column_id}' cannot be deleted because it has associated user stories",
        )

    deleted_position = column.position
    result = delete_entity(session, column)

    # Reorder remaining columns with position greater than the deleted column
    remaining_columns = (
        session.query(ProjectKanbanColumn)
        .filter(
            ProjectKanbanColumn.project_id == project_id,
            ProjectKanbanColumn.position > deleted_position,
        )
        .all()
    )

    for remaining_column in remaining_columns:
        remaining_column.position -= 1

    session.commit()

    return result
