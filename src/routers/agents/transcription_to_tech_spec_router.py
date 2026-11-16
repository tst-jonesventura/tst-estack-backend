from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends

import utils.log as log
from agents.transcription_to_tech_spec_agent import process_in_background
from agents.utils.agent_run_utils import create_agent_logger
from agents.utils.transcription_processing import format_transcription
from database.database import SessionDep
from database.models import User
from schemas.agent_schemas import ExecutionIdPublic
from utils.bearer_auth import check_project_access

logger = log.setup_custom_logger(__name__)
router = APIRouter()


@router.post("/{project_id}/{document_transcription_id}")
async def transcription_to_tech_spec(
    document_transcription_id: str,
    project_id: str,
    session: SessionDep,
    background_tasks: BackgroundTasks,
    user_data: Annotated[User, Depends(check_project_access)],
) -> ExecutionIdPublic:
    transcription_formatted = format_transcription(
        session, project_id, document_transcription_id
    )
    agent_logger = create_agent_logger(
        session=session,
        user_id=user_data.id,
        project_id=project_id,
        agent_run_title=f"Transcrição para Tech Spec {document_transcription_id}",
        queue_key="transcription_to_tech_spec",
        agent_name="transcription_to_tech_spec",
    )
    background_tasks.add_task(
        process_in_background,
        project_id,
        transcription_formatted,
        user_data.id,
        transcription_id=document_transcription_id,
        agent_logger=agent_logger,
    )
    return ExecutionIdPublic(execution_id=agent_logger.agent_run_id)
