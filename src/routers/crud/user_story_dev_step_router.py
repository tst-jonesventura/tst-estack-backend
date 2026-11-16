from typing import List, Annotated

from fastapi import APIRouter, HTTPException, Depends

import utils.log as log
from database.database import SessionDep, create_entity, delete_entity, update_entity
from database.models import UserStoryDevStep
from schemas.user_story_schemas import UserStoryDevStepCreate, UserStoryDevStepPublic
from utils.bearer_auth import check_project_access

router = APIRouter()
logger = log.setup_custom_logger(__name__)
ERROR_MSG_NOT_FOUND = "Dev step not found or access denied."


# CREATE
@router.post("/", response_model=UserStoryDevStepPublic)
def create_user_story_dev_step(
        user_story_id: str,
        dev_step_in: UserStoryDevStepCreate,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    dev_step = UserStoryDevStep(
        order=dev_step_in.order,
        name=dev_step_in.name,
        description=dev_step_in.description,
        user_story_id=user_story_id,
    )
    return create_entity(session, dev_step)


# READ (list)
@router.get("/", response_model=List[UserStoryDevStepPublic])
def list_user_story_dev_steps(
        user_story_id: str,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    results = (
        session.query(UserStoryDevStep)
        .filter(UserStoryDevStep.user_story_id == user_story_id)
        .order_by(UserStoryDevStep.order.asc())
        .all()
    )
    return results


# READ (single)
@router.get("/{dev_step_id}", response_model=UserStoryDevStepPublic)
def get_user_story_dev_step(
        user_story_id: str,
        dev_step_id: str,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    dev_step = session.get(UserStoryDevStep, dev_step_id)
    if not dev_step or dev_step.user_story_id != user_story_id:
        raise HTTPException(status_code=404, detail=ERROR_MSG_NOT_FOUND)
    return dev_step


# UPDATE
@router.put("/{dev_step_id}", response_model=UserStoryDevStepPublic)
def update_user_story_dev_step(
        user_story_id: str,
        dev_step_id: str,
        dev_step_in: UserStoryDevStepCreate,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    dev_step = session.get(UserStoryDevStep, dev_step_id)
    if not dev_step or dev_step.user_story_id != user_story_id:
        raise HTTPException(status_code=404, detail=ERROR_MSG_NOT_FOUND)
    dev_step_data = dev_step_in.model_dump()
    dev_step_data["user_story_id"] = user_story_id
    return update_entity(session, dev_step, dev_step_data)


# DELETE
@router.delete("/{dev_step_id}", response_model=dict)
def delete_user_story_dev_step(
        user_story_id: str,
        dev_step_id: str,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    dev_step = session.get(UserStoryDevStep, dev_step_id)
    if not dev_step or dev_step.user_story_id != user_story_id:
        raise HTTPException(status_code=404, detail=ERROR_MSG_NOT_FOUND)
    return delete_entity(session, dev_step)
