from math import ceil
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import selectinload

import utils.log as log
from agents.utils.agent_run_utils import fail_stale_runs
from database.database import SessionDep
from database.models import AgentRun
from schemas.agent_schemas import AgentRunPublicPage
from schemas.base import Pagination, pagination_params
from utils.bearer_auth import check_project_access

router = APIRouter()
logger = log.setup_custom_logger(__name__)


# GET
@router.get(
    "/",
    response_model=AgentRunPublicPage,
    responses={204: {"description": "No AgentRuns were found"}},
)
def list_agent_runs(
        pagination: Annotated[Pagination, Depends(pagination_params)],
        session: SessionDep,
        project_id: str,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    logger.info(
        f"Listing AgentRuns for project_id: {project_id}, page: {pagination.page}, size: {pagination.size}"
    )

    fail_stale_runs(session, project_id)

    visible_runs = session.query(AgentRun).filter(
        AgentRun.project_id == project_id, AgentRun.is_internal == False
    )

    entity_list = (
        visible_runs.order_by(AgentRun.created_at.desc())
        .limit(pagination.size)
        .offset((pagination.page - 1) * pagination.size)
        .options(
            selectinload(AgentRun.logs),
            selectinload(AgentRun.user),
            selectinload(AgentRun.user_story),
            selectinload(AgentRun.agent),
        )
        .all()
    )

    count = visible_runs.count()
    if not entity_list:
        raise HTTPException(status_code=204, detail="No AgentRuns were found")
    return {
        "pages": ceil(count / pagination.size),
        "data": entity_list,
    }
