from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from database.database import SessionDep
from schemas.project_database_schemas import (
    ProjectDatabaseConnectionCheck,
    ProjectDatabaseCreate,
    ProjectDatabasePublic,
    ProjectDatabaseUpdate,
)
from services.project_database_service import (
    ProjectDatabaseService,
    check_connection,
    target_from_payload,
    target_from_record,
)
from utils.bearer_auth import check_project_access, check_project_ownership

router = APIRouter()

TestConnectionQuery = Annotated[
    bool, Query(description="Testar a conexão e recusar superusuário antes de salvar")
]


@router.get("/", response_model=list[ProjectDatabasePublic])
def list_project_databases(
    project_id: str,
    session: SessionDep,
    has_access: Annotated[bool, Depends(check_project_access)],
):
    return ProjectDatabaseService(session).list_for_project(project_id)


@router.post(
    "/test-connection",
    response_model=ProjectDatabaseConnectionCheck,
    summary="Testar uma conexão antes de salvar",
)
def test_new_database_connection(
    project_id: str,
    payload: ProjectDatabaseCreate,
    has_ownership: Annotated[bool, Depends(check_project_ownership)],
):
    return check_connection(target_from_payload(payload))


@router.post(
    "/",
    response_model=ProjectDatabasePublic,
    status_code=status.HTTP_201_CREATED,
)
def create_project_database(
    project_id: str,
    payload: ProjectDatabaseCreate,
    session: SessionDep,
    has_ownership: Annotated[bool, Depends(check_project_ownership)],
    test_connection: TestConnectionQuery = False,
):
    return ProjectDatabaseService(session).create(project_id, payload, test_connection)


@router.get("/{database_id}", response_model=ProjectDatabasePublic)
def get_project_database(
    project_id: str,
    database_id: str,
    session: SessionDep,
    has_access: Annotated[bool, Depends(check_project_access)],
):
    return ProjectDatabaseService(session).get(project_id, database_id)


@router.post(
    "/{database_id}/test-connection",
    response_model=ProjectDatabaseConnectionCheck,
    summary="Testar a conexão de um banco já cadastrado",
)
def test_saved_database_connection(
    project_id: str,
    database_id: str,
    session: SessionDep,
    has_ownership: Annotated[bool, Depends(check_project_ownership)],
):
    record = ProjectDatabaseService(session).get(project_id, database_id)
    return check_connection(target_from_record(record))


@router.put("/{database_id}", response_model=ProjectDatabasePublic)
def update_project_database(
    project_id: str,
    database_id: str,
    payload: ProjectDatabaseUpdate,
    session: SessionDep,
    has_ownership: Annotated[bool, Depends(check_project_ownership)],
    test_connection: TestConnectionQuery = False,
):
    return ProjectDatabaseService(session).update(
        project_id, database_id, payload, test_connection
    )


@router.delete("/{database_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project_database(
    project_id: str,
    database_id: str,
    session: SessionDep,
    has_ownership: Annotated[bool, Depends(check_project_ownership)],
):
    ProjectDatabaseService(session).delete(project_id, database_id)
