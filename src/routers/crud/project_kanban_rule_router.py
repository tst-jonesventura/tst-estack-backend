from typing import List, Annotated

from fastapi import APIRouter, HTTPException, Depends

import utils.log as log
from database.database import SessionDep, delete_entity
from database.models import ProjectKanbanColumnRule
from schemas.user_story_schemas import (
    KanbanHandlersPublic,
    ProjectKanbanColumnRuleCreateUpdate,
    ProjectKanbanColumnRulePublic,
)
from services.kanban_service import kanban_service
from utils.bearer_auth import retrieve_user_data

router = APIRouter()
logger = log.setup_custom_logger(__name__)
RULE_NOT_FOUND = "Rule not found"


@router.get("/handlers", response_model=KanbanHandlersPublic)
def get_kanban_handlers(
        user_data: Annotated[bool, Depends(retrieve_user_data)],
):
    logger.info("Received request to get available kanban handler keys")
    return KanbanHandlersPublic(handler_keys=kanban_service.get_handler_keys())


@router.get(
    "/{project_kanban_column_rule_id}",
    response_model=ProjectKanbanColumnRulePublic,
    responses={404: {"description": "Rule not found"}},
)
def get_kanban_column_rules_by_id(
        session: SessionDep,
        project_kanban_column_rule_id: str,
        user_data: Annotated[bool, Depends(retrieve_user_data)],
):
    logger.info(
        f"Received request to get rule info by id: {project_kanban_column_rule_id}"
    )

    project_kanban_rule = (
        session.query(ProjectKanbanColumnRule)
        .filter(ProjectKanbanColumnRule.id == project_kanban_column_rule_id)
        .first()
    )

    if not project_kanban_rule:
        raise HTTPException(status_code=404, detail=RULE_NOT_FOUND)

    return project_kanban_rule


@router.get(
    "/kanban_column/{project_kanban_column_id}",
    response_model=List[ProjectKanbanColumnRulePublic],
)
def get_kanban_column_rules_by_kanban_column_id(
        session: SessionDep,
        project_kanban_column_id: str,
        user_data: Annotated[bool, Depends(retrieve_user_data)],
):
    logger.info(
        f"Received request to get rules of project kanban column with ID: {project_kanban_column_id}"
    )

    project_kanban_rule_list = (
        session.query(ProjectKanbanColumnRule)
        .filter(
            ProjectKanbanColumnRule.target_kanban_column_id == project_kanban_column_id
        )
        .order_by(ProjectKanbanColumnRule.order.asc())
        .all()
    )
    return [
        ProjectKanbanColumnRulePublic.model_validate(context)
        for context in project_kanban_rule_list
    ]


@router.post("/", response_model=ProjectKanbanColumnRulePublic)
def create_kanban_column_rule(
        session: SessionDep,
        rule: ProjectKanbanColumnRuleCreateUpdate,
        user_data: Annotated[bool, Depends(retrieve_user_data)],
):
    logger.info(
        f"Received request to create a new kanban column rule for kanban column with id: {rule.target_kanban_column_id}"
    )

    new_rule = ProjectKanbanColumnRule(
        action_type=rule.action_type,
        provider=rule.provider,
        handler_key=rule.handler_key,
        error_message=rule.error_message,
        payload_schema=rule.payload_schema,
        is_blocking=rule.is_blocking,
        order=rule.order,
        target_kanban_column_id=rule.target_kanban_column_id,
    )
    session.add(new_rule)
    session.commit()
    session.refresh(new_rule)
    return new_rule


@router.put(
    "/{project_kanban_column_rule_id}",
    response_model=ProjectKanbanColumnRulePublic,
    responses={404: {"description": "Rule not found"}},
)
def update_kanban_column_rule(
        session: SessionDep,
        project_kanban_column_rule_id: str,
        rule_update: ProjectKanbanColumnRuleCreateUpdate,
        user_data: Annotated[bool, Depends(retrieve_user_data)],
):
    logger.info(
        f"Received request to update kanban column rule with id: {project_kanban_column_rule_id}"
    )

    rule = (
        session.query(ProjectKanbanColumnRule)
        .filter(ProjectKanbanColumnRule.id == project_kanban_column_rule_id)
        .first()
    )

    if not rule:
        raise HTTPException(status_code=404, detail=RULE_NOT_FOUND)

    rule.action_type = rule_update.action_type
    rule.provider = rule_update.provider
    rule.handler_key = rule_update.handler_key
    rule.error_message = rule_update.error_message
    rule.payload_schema = rule_update.payload_schema
    rule.is_blocking = rule_update.is_blocking
    rule.order = rule_update.order
    rule.target_kanban_column_id = rule_update.target_kanban_column_id

    session.commit()
    session.refresh(rule)
    return rule


@router.delete(
    "/{project_kanban_column_rule_id}",
    response_model=dict,
    responses={404: {"description": "Rule not found"}},
)
def delete_kanban_column_rule(
        session: SessionDep,
        project_kanban_column_rule_id: str,
        user_data: Annotated[bool, Depends(retrieve_user_data)],
):
    logger.info(
        f"Received request to delete kanban column rule with id: {project_kanban_column_rule_id}"
    )

    rule = (
        session.query(ProjectKanbanColumnRule)
        .filter(ProjectKanbanColumnRule.id == project_kanban_column_rule_id)
        .first()
    )

    if not rule:
        raise HTTPException(status_code=404, detail=RULE_NOT_FOUND)

    return delete_entity(session, rule)
