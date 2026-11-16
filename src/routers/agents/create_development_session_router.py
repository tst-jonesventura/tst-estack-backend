from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from agents.development_session_agent import (
    export_prompt,
    process_in_background,
)
from database.database import SessionDep
from database.models import ProjectRepository, User, UserStory
from schemas.agent_schemas import ExecutionIdPublic
from utils import log
from utils.bearer_auth import retrieve_user_data
from agents.utils.agent_run_utils import create_agent_logger
from agents.utils.agent_factory import get_agent_info

logger = log.setup_custom_logger(__name__)
router = APIRouter()


@router.post(
    "/{user_story_id}",
    responses={422: {"description": "Git repositories not found"}},
)
async def create_development_session(
    user_story_id: str,
    session: SessionDep,
    background_tasks: BackgroundTasks,
    user_data: Annotated[User, Depends(retrieve_user_data)],
    response_model=ExecutionIdPublic,
) -> ExecutionIdPublic:
    logger.info("Creating development session for user story ID %s", user_story_id)

    project_id = (
        session.query(UserStory).filter_by(id=user_story_id).scalar().project_id
    )
    # check if there are repositories on the project
    repositories_count = (
        session.query(ProjectRepository)
        .where(ProjectRepository.project_id == project_id)
        .count()
    )
    if repositories_count == 0:
        logger.warning(
            "No repositories found for project ID %s. Cannot create development session.",
            project_id,
        )
        raise HTTPException(
            status_code=422, detail="Repositories not found for this project"
        )
    agent_info = get_agent_info(session, "devin-api", project_id=project_id)
    logger.info(f"Resolved agent info for agent name {agent_info.model if agent_info else 'devin'}")

    agent_logger = create_agent_logger(
        session=session,
        user_id=user_data.id,
        project_id=project_id,
        agent_run_title=f"Criação de sessão de desenvolvimento para User Story {user_story_id}",
        user_story_id=user_story_id,
        model=agent_info.model if agent_info else "devin",
        agent_id=agent_info.id if agent_info else None,
        agent_name="devin-api",
    )

    logger.info("Initializing background task for development session creation")
    background_tasks.add_task(process_in_background, session, user_data.id, user_story_id, agent_logger)
    return ExecutionIdPublic(execution_id=agent_logger.agent_run_id)


@router.get("/prompt/{user_story_id}")
async def get_development_session_prompt(
    user_story_id: str,
    session: SessionDep,
    user_data: Annotated[User, Depends(retrieve_user_data)],
    response_model=str,
):
    return export_prompt(session, user_data.id, user_story_id)
