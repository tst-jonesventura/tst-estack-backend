from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends

import utils.log as log
from agents.repo_to_tech_spec_agent import create_tech_specs_from_repo_in_background
from database.models import User
from schemas.agent_schemas import ExecutionIdPublic
from utils.bearer_auth import check_project_access

logger = log.setup_custom_logger(__name__)
router = APIRouter()


@router.post("/{project_id}")
async def repo_to_tech_spec(
        project_id: str,
        repo_url: str,
        background_tasks: BackgroundTasks,
        user_data: Annotated[User, Depends(check_project_access)],
) -> ExecutionIdPublic:
    background_tasks.add_task(
        create_tech_specs_from_repo_in_background,
        project_id,
        repo_url,
        user_data.id,
    )
    return ExecutionIdPublic(execution_id="")
