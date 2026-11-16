from typing import Annotated, List

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import joinedload

import utils.log as log
from agents.labeler_agent import tag_and_commit_in_background
from agents.utils.agent_run_utils import create_agent_logger
from database.database import SessionDep, create_entity, delete_entity, update_entity
from database.models import DocumentTranscription, User
from schemas import transcription_schemas
from utils.bearer_auth import check_project_access
from utils.validation import validate_document_transcription_access

router = APIRouter()
logger = log.setup_custom_logger(__name__)


# CREATE
@router.post("/transcriptions/")
def create_document_transcription(
    project_id: str,
    transcription_in: transcription_schemas.DocumentTranscriptionCreate,
    session: SessionDep,
    background_tasks: BackgroundTasks,
    user_data: Annotated[User, Depends(check_project_access)],
) -> transcription_schemas.CreateTranscriptionResponse:
    transcription = DocumentTranscription(
        title=transcription_in.title,
        content=transcription_in.content,
        project_id=project_id,
    )
    create_entity(session, transcription)
    agent_logger = create_agent_logger(
        session,
        user_data.id,
        project_id,
        "Adicionando transcrição",
        agent_name="labeler",
    )
    background_tasks.add_task(
        tag_and_commit_in_background,
        project_id=project_id,
        user_id=user_data.id,
        input_id=transcription.id,
        input_type="transcription",
        agent_run_id=agent_logger.agent_run_id,
    )
    return transcription_schemas.CreateTranscriptionResponse(
        transcription_id=transcription.id, execution_id=agent_logger.agent_run_id
    )


# READ (list)
@router.get(
    "/transcriptions/",
    response_model=List[transcription_schemas.DocumentTranscriptionPublic],
)
def list_document_transcriptions(
    project_id: str,
    session: SessionDep,
    user_data: Annotated[User, Depends(check_project_access)],
):
    results = (
        session.query(DocumentTranscription)
        .options(joinedload(DocumentTranscription.tags))
        .order_by(DocumentTranscription.created_at.desc())
        .filter(DocumentTranscription.project_id == project_id)
        .all()
    )
    return results


# READ (single)
@router.get(
    "/transcriptions/{transcription_id}",
    response_model=transcription_schemas.DocumentTranscriptionPublic,
)
def get_document_transcription(
    project_id: str,
    transcription_id: str,
    session: SessionDep,
    user_data: Annotated[User, Depends(check_project_access)],
):
    transcription = validate_document_transcription_access(
        session, project_id, transcription_id
    )
    return transcription


# UPDATE
@router.put(
    "/transcriptions/{transcription_id}",
    response_model=transcription_schemas.DocumentTranscriptionPublic,
)
def update_document_transcription(
    project_id: str,
    transcription_id: str,
    transcription_in: transcription_schemas.DocumentTranscriptionCreate,
    session: SessionDep,
    user_data: Annotated[User, Depends(check_project_access)],
):
    transcription = validate_document_transcription_access(
        session, project_id, transcription_id
    )

    transcription_data = transcription_in.model_dump()
    transcription_data["project_id"] = project_id
    return update_entity(session, transcription, transcription_data)


# DELETE
@router.delete("/transcriptions/{transcription_id}", response_model=dict)
def delete_document_transcription(
    project_id: str,
    transcription_id: str,
    session: SessionDep,
    user_data: Annotated[User, Depends(check_project_access)],
):
    transcription = validate_document_transcription_access(
        session, project_id, transcription_id
    )

    return delete_entity(session, transcription)
