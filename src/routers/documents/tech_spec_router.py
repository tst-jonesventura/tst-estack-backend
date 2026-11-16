from datetime import datetime, timezone
from typing import Annotated, List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import joinedload

import utils.log as log
from agents.labeler_agent import tag_and_commit_in_background
from agents.utils.agent_run_utils import create_agent_logger
from database.database import SessionDep, create_entity
from database.models import DocumentTechSpec, DocumentTechSpecVersion, TechSpecTag, User
from schemas.tech_spec_schemas import (
    DocumentTechSpecCreate,
    DocumentTechSpecPublic,
    DocumentTechSpecUpdate,
    DocumentTechSpecVersionPublic,
)
from utils.bearer_auth import check_project_access
from utils.validation import validate_entity_exists, validate_project_access

router = APIRouter(tags=["tech-specs"])
logger = log.setup_custom_logger(__name__)
EXCEPTION_MSG_NOT_FOUND = "DocumentTechSpec not found or access denied."


# CREATE
@router.post("/", response_model=DocumentTechSpecPublic)
def create_document_tech_spec(
        project_id: str,
        tech_spec_in: DocumentTechSpecCreate,
        session: SessionDep,
        background_tasks: BackgroundTasks,
        user_data: Annotated[User, Depends(check_project_access)],
):
    tech_spec = DocumentTechSpec(
        title=tech_spec_in.title,
        content=tech_spec_in.content,
        project_id=project_id,
        user_id=user_data.id,
        log="Especificação técnica criada por usuário",
        created_at=datetime.now(timezone.utc),
    )
    created_tech_spec = create_entity(session, tech_spec)
    agent_logger = create_agent_logger(
        session=session,
        user_id=user_data.id,
        project_id=project_id,
        agent_run_title=f"Criação de tags para tech spec {created_tech_spec.title}",
        agent_name="labeler",
    )
    background_tasks.add_task(
        tag_and_commit_in_background,
        project_id=project_id,
        user_id=user_data.id,
        input_id=created_tech_spec.id,
        input_type="tech_spec",
        agent_run_id=agent_logger.agent_run_id,
    )
    return created_tech_spec


# READ (list)
@router.get("/", response_model=List[DocumentTechSpecPublic])
def list_document_tech_specs(
        project_id: str,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    validate_project_access(session, project_id)

    results = (
        session.query(DocumentTechSpec)
        .options(joinedload(DocumentTechSpec.tags))
        .filter(DocumentTechSpec.project_id == project_id)
        .order_by(DocumentTechSpec.created_at.desc())
        .all()
    )
    return results


# READ (single)
@router.get(
    "/{tech_spec_id}",
    response_model=DocumentTechSpecPublic,
    responses={404: {"description": EXCEPTION_MSG_NOT_FOUND}},
)
def get_document_tech_spec(
        project_id: str,
        tech_spec_id: str,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    tech_spec = validate_entity_exists(
        session, DocumentTechSpec, tech_spec_id, "DocumentTechSpec"
    )
    if tech_spec.project_id != project_id:
        raise HTTPException(status_code=404, detail=EXCEPTION_MSG_NOT_FOUND)
    return tech_spec


@router.get(
    "/{tech_spec_id}/versions",
    response_model=List[DocumentTechSpecVersionPublic],
    responses={404: {"description": EXCEPTION_MSG_NOT_FOUND}},
)
def get_document_tech_spec_versions(
        project_id: str,
        tech_spec_id: str,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    tech_spec = session.get(DocumentTechSpec, tech_spec_id)
    validate_project_access(session, project_id)
    if not tech_spec or tech_spec.project_id != project_id:
        raise HTTPException(status_code=404, detail=EXCEPTION_MSG_NOT_FOUND)

    versions = (
        session.query(DocumentTechSpecVersion)
        .filter(DocumentTechSpecVersion.id == tech_spec_id)
        .order_by(DocumentTechSpecVersion.transaction_id.desc())  # type: ignore
        .all()
    )

    versions_public = [
        DocumentTechSpecVersionPublic.model_validate(v) for v in versions
    ]
    return versions_public


# UPDATE
@router.put(
    "/{tech_spec_id}",
    response_model=DocumentTechSpecPublic,
    responses={404: {"description": EXCEPTION_MSG_NOT_FOUND}},
)
def update_document_tech_spec(
        project_id: str,
        tech_spec_id: str,
        tech_spec_in: DocumentTechSpecUpdate,
        session: SessionDep,
        background_tasks: BackgroundTasks,
        user_data: Annotated[User, Depends(check_project_access)],
):
    tech_spec = validate_entity_exists(
        session, DocumentTechSpec, tech_spec_id, "DocumentTechSpec"
    )
    validate_project_access(session, project_id)
    if tech_spec.project_id != project_id:
        raise HTTPException(status_code=404, detail=EXCEPTION_MSG_NOT_FOUND)

    tech_spec_data = tech_spec_in.model_dump()
    # Only update fields present in the payload
    for key, value in tech_spec_data.items():
        if value is not None:
            setattr(tech_spec, key, value)
    # Optionally update metadata (log, user_id, timestamp) if available
    # tech_spec.user_id = ... # set from context if needed
    tech_spec.log = "Especificação técnica atualizada por usuário"
    tech_spec.created_at = datetime.now(timezone.utc)
    tech_spec.user_id = user_data.id
    session.commit()
    agent_logger = create_agent_logger(
        session=session,
        user_id=user_data.id,
        project_id=project_id,
        agent_run_title=f"Criação de tags para tech spec {tech_spec.title}",
        agent_name="labeler",
    )
    background_tasks.add_task(
        tag_and_commit_in_background,
        project_id=project_id,
        user_id=user_data.id,
        input_id=tech_spec_id,
        input_type="tech_spec",
        agent_run_id=agent_logger.agent_run_id,
    )

    return tech_spec


# DELETE
@router.delete(
    "/{tech_spec_id}",
    response_model=dict,
    responses={404: {"description": EXCEPTION_MSG_NOT_FOUND}},
)
def delete_document_tech_spec(
        project_id: str,
        tech_spec_id: str,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    tech_spec = validate_entity_exists(
        session, DocumentTechSpec, tech_spec_id, "DocumentTechSpec"
    )
    if tech_spec.project_id != project_id:
        raise HTTPException(status_code=404, detail=EXCEPTION_MSG_NOT_FOUND)
    session.query(TechSpecTag).filter_by(tech_spec_id=tech_spec.id).delete(
        synchronize_session=False
    )

    session.commit()
    session.delete(tech_spec)
    session.commit()
    return {"ok": True}
