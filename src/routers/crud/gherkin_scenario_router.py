from typing import List, Annotated

from fastapi import APIRouter, HTTPException, Depends

import utils.log as log
from database.database import SessionDep, create_entity, delete_entity, update_entity
from database.models import GherkinScenario
from schemas.user_story_schemas import (
    GherkinScenarioCreate,
    GherkinScenarioPublic,
)
from utils.bearer_auth import check_project_access

router = APIRouter()
logger = log.setup_custom_logger(__name__)
ERROR_MSG_NOT_FOUND = "Gherkin scenario not found or access denied."
RESPONSES_404 = {404: {"description": ERROR_MSG_NOT_FOUND}}


# CREATE
@router.post("/", response_model=GherkinScenarioPublic)
def create_gherkin_scenario(
        rule_id: str,
        scenario_in: GherkinScenarioCreate,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    scenario = GherkinScenario(
        index=scenario_in.index,
        title=scenario_in.title,
        text=scenario_in.text,
        is_done=scenario_in.is_done,
        gherkin_rule_id=rule_id,
    )
    return create_entity(session, scenario)


# READ (list)
@router.get("/", response_model=List[GherkinScenarioPublic])
def list_gherkin_scenarios(
        rule_id: str,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    results = (
        session.query(GherkinScenario)
        .filter(GherkinScenario.gherkin_rule_id == rule_id)
        .order_by(GherkinScenario.index.asc())
        .all()
    )
    return results


# READ (single)
@router.get(
    "/{scenario_id}",
    response_model=GherkinScenarioPublic,
    responses=RESPONSES_404,  # type: ignore
)  # type: ignore
def get_gherkin_scenario(
        rule_id: str,
        scenario_id: str,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    scenario = session.get(GherkinScenario, scenario_id)
    if not scenario or scenario.gherkin_rule_id != rule_id:
        raise HTTPException(status_code=404, detail=ERROR_MSG_NOT_FOUND)
    return scenario


# UPDATE
@router.put(
    "/{scenario_id}",
    response_model=GherkinScenarioPublic,
    responses=RESPONSES_404,  # type: ignore
)  # type: ignore
def update_gherkin_scenario(
        rule_id: str,
        scenario_id: str,
        scenario_in: GherkinScenarioCreate,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    scenario = session.get(GherkinScenario, scenario_id)
    if not scenario or scenario.gherkin_rule_id != rule_id:
        raise HTTPException(status_code=404, detail=ERROR_MSG_NOT_FOUND)
    scenario_data = scenario_in.model_dump()
    scenario_data["gherkin_rule_id"] = rule_id
    return update_entity(session, scenario, scenario_data)


# DELETE
@router.delete("/{scenario_id}", response_model=dict, responses=RESPONSES_404)  # type: ignore
def delete_gherkin_scenario(
        rule_id: str,
        scenario_id: str,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    scenario = session.get(GherkinScenario, scenario_id)
    if not scenario or scenario.gherkin_rule_id != rule_id:
        raise HTTPException(status_code=404, detail=ERROR_MSG_NOT_FOUND)
    return delete_entity(session, scenario)
