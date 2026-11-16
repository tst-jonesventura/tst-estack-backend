from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

import utils.log as log
from database.database import SessionDep
from database.models import User, UserStory
from schemas.agent_schemas import ExecutionIdPublic
from services.agent_service import create_user_story_dev_steps_in_background
from utils.bearer_auth import retrieve_user_data


logger = log.setup_custom_logger(__name__)
router = APIRouter()


@router.post(
    "/{user_story_id}",
    responses={404: {"description": "UserStory not found or Project not found"}},
)
async def user_story_to_dev_steps(
        user_story_id: str,
        session: SessionDep,
        background_tasks: BackgroundTasks,
        user_data: Annotated[User, Depends(retrieve_user_data)],
) -> ExecutionIdPublic:
    logger.info("Starting dev steps generation for UserStory ID: %s", user_story_id)
    user_story = (
        session.query(UserStory)
        .filter_by(id=user_story_id)
        .scalar()
    )

    if not user_story:
        logger.error("UserStory not found.")
        raise HTTPException(status_code=404, detail="UserStory not found")

    project = user_story.project
    if not project:
        logger.error("Project for UserStory not found.")
        raise HTTPException(status_code=404, detail="Project not found")

    background_tasks.add_task(
        create_user_story_dev_steps_in_background,
        project_id=project.id,
        user_id=user_data.id,
        story_id=user_story.id,
    )
    return ExecutionIdPublic(execution_id="")