"""Database connectors: managed by admins, chosen by projects, called by the connectors."""
import asyncio
import json
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from database.database import SessionDep, create_session
from database.models import DatabaseConnector, Organization, Project
from schemas.database_connector_schemas import (
    ConnectorHeartbeat,
    ConnectorJob,
    ConnectorJobResult,
    DatabaseConnectorCreate,
    DatabaseConnectorPublic,
    DatabaseConnectorWithToken,
    OrganizationConnectors,
)
from services import database_connector_service as connectors
from utils.bearer_auth import check_project_access, retrieve_admin_data

overview_router = APIRouter()
admin_router = APIRouter()
project_router = APIRouter()
connector_router = APIRouter()

connector_bearer = HTTPBearer(auto_error=False)

INVALID_TOKEN = "Token de conector inválido ou revogado."


def to_public(connector: DatabaseConnector) -> DatabaseConnectorPublic:
    return DatabaseConnectorPublic(
        id=connector.id,
        organization_id=connector.organization_id,
        name=connector.name,
        token_hint=connector.token_hint,
        version=connector.version,
        last_seen_at=connector.last_seen_at,
        online=connectors.is_online(connector),
        databases=connectors.reported_databases(connector),
        is_active=connector.is_active,
        created_at=connector.created_at,
    )


def _get_connector(session, organization_id: str, connector_id: str) -> DatabaseConnector:
    connector = session.get(DatabaseConnector, connector_id)
    if connector is None or connector.organization_id != organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conector não encontrado.")
    return connector


# region Admin
@overview_router.get("/", response_model=list[OrganizationConnectors])
def list_connectors_by_organization(
    session: SessionDep,
    admin: Annotated[object, Depends(retrieve_admin_data)],
):
    organizations = session.query(Organization).order_by(Organization.name).all()
    found = session.query(DatabaseConnector).order_by(DatabaseConnector.name).all()
    by_organization: dict[str, list[DatabaseConnectorPublic]] = {}
    for connector in found:
        by_organization.setdefault(connector.organization_id, []).append(to_public(connector))
    return [
        OrganizationConnectors(
            organization_id=organization.id,
            organization_name=organization.name,
            connectors=by_organization.get(organization.id, []),
        )
        for organization in organizations
    ]


@admin_router.post(
    "/", response_model=DatabaseConnectorWithToken, status_code=status.HTTP_201_CREATED
)
def create_database_connector(
    organization_id: str,
    payload: DatabaseConnectorCreate,
    session: SessionDep,
    admin: Annotated[object, Depends(retrieve_admin_data)],
):
    connector, token = connectors.create_connector(session, organization_id, payload.name)
    return DatabaseConnectorWithToken(**to_public(connector).model_dump(), token=token)


@admin_router.get("/", response_model=list[DatabaseConnectorPublic])
def list_database_connectors(
    organization_id: str,
    session: SessionDep,
    admin: Annotated[object, Depends(retrieve_admin_data)],
):
    found = (
        session.query(DatabaseConnector)
        .filter(DatabaseConnector.organization_id == organization_id)
        .order_by(DatabaseConnector.name)
        .all()
    )
    return [to_public(connector) for connector in found]


@admin_router.post("/{connector_id}/rotate-token", response_model=DatabaseConnectorWithToken)
def rotate_database_connector_token(
    organization_id: str,
    connector_id: str,
    session: SessionDep,
    admin: Annotated[object, Depends(retrieve_admin_data)],
):
    connector = _get_connector(session, organization_id, connector_id)
    token = connectors.rotate_token(session, connector)
    return DatabaseConnectorWithToken(**to_public(connector).model_dump(), token=token)


@admin_router.delete("/{connector_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_database_connector(
    organization_id: str,
    connector_id: str,
    session: SessionDep,
    admin: Annotated[object, Depends(retrieve_admin_data)],
):
    connector = _get_connector(session, organization_id, connector_id)
    connector.is_active = False
    session.commit()


# endregion


# region Project
@project_router.get("/", response_model=list[DatabaseConnectorPublic])
def list_project_database_connectors(
    project_id: str,
    session: SessionDep,
    has_access: Annotated[bool, Depends(check_project_access)],
):
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Projeto não encontrado.")
    found = (
        session.query(DatabaseConnector)
        .filter(
            DatabaseConnector.organization_id == project.organization_id,
            DatabaseConnector.is_active == True,
        )
        .order_by(DatabaseConnector.name)
        .all()
    )
    return [to_public(connector) for connector in found]


# endregion


# region Connector
def _authenticate(token: str | None) -> str | None:
    with create_session() as session:
        connector = connectors.authenticate(session, token)
        return connector.id if connector else None


async def current_connector_id(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(connector_bearer)],
) -> str:
    """The calling connector, resolved with a short session so a long poll holds no connection."""
    token = credentials.credentials if credentials else None
    connector_id = await run_in_threadpool(_authenticate, token)
    if connector_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, INVALID_TOKEN)
    return connector_id


def _heartbeat(connector_id: str, payload: ConnectorHeartbeat) -> None:
    with create_session() as session:
        connector = session.get(DatabaseConnector, connector_id)
        connectors.record_heartbeat(
            session,
            connector,
            payload.version,
            [database.model_dump() for database in payload.databases],
        )
        connectors.purge_finished_jobs(session)


def _claim(connector_id: str) -> list[ConnectorJob]:
    with create_session() as session:
        connector = session.get(DatabaseConnector, connector_id)
        return [
            ConnectorJob(
                id=job.id,
                database_alias=job.database_alias,
                operation=job.operation,
                arguments=json.loads(job.arguments),
            )
            for job in connectors.claim_jobs(session, connector)
        ]


def _complete(connector_id: str, job_id: str, payload: ConnectorJobResult) -> bool:
    with create_session() as session:
        connector = session.get(DatabaseConnector, connector_id)
        return connectors.complete_job(session, connector, job_id, payload.result, payload.error)


@connector_router.post("/heartbeat", status_code=status.HTTP_204_NO_CONTENT)
async def connector_heartbeat(
    payload: ConnectorHeartbeat,
    connector_id: Annotated[str, Depends(current_connector_id)],
):
    await run_in_threadpool(_heartbeat, connector_id, payload)


@connector_router.get("/jobs", response_model=list[ConnectorJob])
async def poll_connector_jobs(
    connector_id: Annotated[str, Depends(current_connector_id)],
    wait: Annotated[int, Query(ge=0, le=connectors.LONG_POLL_MAX_SECONDS)] = (
        connectors.LONG_POLL_MAX_SECONDS
    ),
):
    deadline = time.monotonic() + wait
    while True:
        jobs = await run_in_threadpool(_claim, connector_id)
        if jobs or time.monotonic() >= deadline:
            return jobs
        await asyncio.sleep(connectors.LONG_POLL_INTERVAL_SECONDS)


@connector_router.post("/jobs/{job_id}/result", status_code=status.HTTP_204_NO_CONTENT)
async def submit_connector_job_result(
    job_id: str,
    payload: ConnectorJobResult,
    connector_id: Annotated[str, Depends(current_connector_id)],
):
    accepted = await run_in_threadpool(_complete, connector_id, job_id, payload)
    if not accepted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job não encontrado ou já encerrado.")


# endregion
