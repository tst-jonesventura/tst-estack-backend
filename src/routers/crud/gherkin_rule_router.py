from typing import List, Annotated

from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import joinedload

import utils.log as log
from database.database import SessionDep, create_entity, delete_entity, update_entity
from database.models import GherkinRule
from schemas.user_story_schemas import (
    GherkinRuleCreate,
    GherkinRuleFullPublic,
)
from utils.bearer_auth import check_project_access

router = APIRouter()
logger = log.setup_custom_logger(__name__)
ERROR_MSG_NOT_FOUND = "Gherkin rule not found or access denied."
RESPONSES_404 = {404: {"description": ERROR_MSG_NOT_FOUND}}


# CREATE
@router.post("/", response_model=GherkinRuleFullPublic)
def create_gherkin_rule(
        user_story_id: str,
        rule_in: GherkinRuleCreate,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    rule = GherkinRule(
        index=rule_in.index,
        title=rule_in.title,
        background=rule_in.background,
        user_story_id=user_story_id,
    )
    created = create_entity(session, rule)
    return GherkinRuleFullPublic.model_validate(created)


# READ (list)
@router.get("/", response_model=List[GherkinRuleFullPublic])
def list_gherkin_rules(
        user_story_id: str,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    results = (
        session.query(GherkinRule)
        .filter(GherkinRule.user_story_id == user_story_id)
        .options(joinedload(GherkinRule.scenarios))
        .order_by(GherkinRule.index.asc())
        .all()
    )
    return [GherkinRuleFullPublic.model_validate(r) for r in results]


# READ (single)
@router.get("/{rule_id}", response_model=GherkinRuleFullPublic, responses=RESPONSES_404)  # type: ignore
def get_gherkin_rule(
        user_story_id: str,
        rule_id: str,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    rule = (
        session.query(GherkinRule)
        .filter(GherkinRule.id == rule_id)
        .options(joinedload(GherkinRule.scenarios))
        .first()
    )
    if not rule or rule.user_story_id != user_story_id:
        raise HTTPException(status_code=404, detail=ERROR_MSG_NOT_FOUND)
    return GherkinRuleFullPublic.model_validate(rule)


# UPDATE
@router.put("/{rule_id}", response_model=GherkinRuleFullPublic, responses=RESPONSES_404)  # type: ignore
def update_gherkin_rule(
        user_story_id: str,
        rule_id: str,
        rule_in: GherkinRuleCreate,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    rule = session.get(GherkinRule, rule_id)
    if not rule or rule.user_story_id != user_story_id:
        raise HTTPException(status_code=404, detail=ERROR_MSG_NOT_FOUND)
    rule_data = rule_in.model_dump()
    rule_data["user_story_id"] = user_story_id
    updated = update_entity(session, rule, rule_data)
    return GherkinRuleFullPublic.model_validate(updated)


# DELETE
@router.delete("/{rule_id}", response_model=dict, responses=RESPONSES_404)  # type: ignore
def delete_gherkin_rule(
        user_story_id: str,
        rule_id: str,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    rule = session.get(GherkinRule, rule_id)
    if not rule or rule.user_story_id != user_story_id:
        raise HTTPException(status_code=404, detail=ERROR_MSG_NOT_FOUND)
    return delete_entity(session, rule)
