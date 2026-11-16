from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends

import utils.log as log
from agents.labeler_agent import tag_and_commit_in_background
from agents.utils.agent_run_utils import create_agent_logger
from database.database import SessionDep
from database.models import User
from schemas.agent_schemas import ExecutionIdPublic
from utils.bearer_auth import check_project_access

logger = log.setup_custom_logger(__name__)
router = APIRouter()


@router.post("/{project_id}/{id_type}/{id}")
async def label(
    project_id: str,
    id_type: Literal["transcription", "tech_spec", "user_story"],
    id: str,
    session: SessionDep,
    background_tasks: BackgroundTasks,
    user_data: Annotated[User, Depends(check_project_access)],
) -> ExecutionIdPublic:
    agent_logger = create_agent_logger(
        session=session,
        user_id=user_data.id,
        project_id=project_id,
        agent_run_title=f"Criação de tags {id_type}",
        agent_name="labeler",
    )

    background_tasks.add_task(
        tag_and_commit_in_background,
        project_id=project_id,
        user_id=user_data.id,
        input_id=id,
        input_type=id_type,
        agent_run_id=agent_logger.agent_run_id,
    )
    return ExecutionIdPublic(execution_id=agent_logger.agent_run_id)
