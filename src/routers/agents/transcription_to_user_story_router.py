from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends

from agents import labeler_agent, transcription_to_user_story_agent
from agents.utils.agent_deps import AgentDeps
from agents.utils.agent_run_utils import AgentLogger, create_agent_logger
from agents.utils.transcription_processing import format_transcription
from database.database import SessionDep, create_session
from database.models import User
from schemas.agent_schemas import ExecutionIdPublic
from services.agent_queue_service import wait_for_sequential_slot
from services.sync_service import SyncService
from utils import log
from utils.bearer_auth import check_project_access

logger = log.setup_custom_logger(__name__)
router = APIRouter()


def transcription_to_user_stories_flow(
        project_id: str,
        user_id: str,
        transcription_formatted: str,
        document_transcription_id: str,
        agent_run_id: str,
):
    with create_session() as session:
        sync_service = SyncService(session)

        agent_logger = AgentLogger(agent_run_id)
        deps = AgentDeps(
            project_id=project_id,
            agent_logger=agent_logger,
            user_id=user_id,
            input_id=document_transcription_id,
            input_type="transcription",
            session=session,
        )
        wait_for_sequential_slot(project_id, "transcription_to_story", agent_run_id)
        user_stories_db = transcription_to_user_story_agent.run(
            transcription_formatted, deps
        )
        logger.info(f"{len(user_stories_db)} user stories criadas a partir da transcrição.")
        user_stories_ids = [us.id for us in user_stories_db]
        session.add_all(user_stories_db)
        session.commit()

        for user_story_id in user_stories_ids:
            sync_service.sync_create(project_id, user_story_id)

            deps.input_id = user_story_id
            deps.input_type = "user_story"
            tags = labeler_agent.run(deps=deps)
            session.add_all(tags)
            session.commit()
        logger.info(f"Tags criadas para {len(user_stories_ids)} user stories.")


@router.post("/{project_id}/{document_transcription_id}")
async def transcription_to_user_stories(
        project_id: str,
        document_transcription_id: str,
        session: SessionDep,
        background_tasks: BackgroundTasks,
        user_data: Annotated[User, Depends(check_project_access)],
        response_model=ExecutionIdPublic,
) -> ExecutionIdPublic:
    transcription_formatted = format_transcription(
        session, project_id, document_transcription_id
    )
    agent_logger = create_agent_logger(
        session=session,
        user_id=user_data.id,
        project_id=project_id,
        agent_run_title=f"Transcrição para User Story {document_transcription_id}",
        queue_key="transcription_to_story",
        agent_name="transcription_to_story",
    )

    background_tasks.add_task(
        transcription_to_user_stories_flow,
        project_id=project_id,
        user_id=user_data.id,
        transcription_formatted=transcription_formatted,
        document_transcription_id=document_transcription_id,
        agent_run_id=agent_logger.agent_run_id,
    )
    return ExecutionIdPublic(execution_id=agent_logger.agent_run_id)
