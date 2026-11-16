from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from database.database import SessionDep
from schemas.integration_schemas import (
    JiraGetColumnsPayload,
    JiraIntegrationConfigCreate,
    JiraIntegrationConfigPublic,
    JiraIntegrationConfigUpdate,
    JiraTestConnectionPayload,
)
from services.integration_service import IntegrationService
from utils import log
from utils.bearer_auth import (
    check_project_access,
)

logger = log.setup_custom_logger(__name__)
router = APIRouter()


@router.post(
    "/{provider}/test-connection",
    response_model=list[dict[str, Any]],
    summary="Test connection and list projects from provider",
)
@router.post(
    "/{provider}/projects",
    response_model=list[dict[str, Any]],
    summary="Test connection and list projects from provider",
)
async def test_connection_and_list_projects(
        project_id: str,
        provider: str,
        payload: JiraTestConnectionPayload,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    logger.info(f"Testing connection and fetching projects for provider {provider}")
    service = IntegrationService(session)
    return await service.test_connection_and_get_projects(
        provider=provider, payload=payload
    )


@router.post(
    "/{provider}/columns",
    response_model=dict[str, Any],
    summary="Get board columns configuration for a project from provider",
)
@router.post(
    "/{provider}/board-configuration",
    response_model=dict[str, Any],
    summary="Get board configuration for a project from provider",
)
async def get_provider_columns(
        project_id: str,
        provider: str,
        payload: JiraGetColumnsPayload,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    logger.info(f"Fetching columns / board configuration for provider {provider}")
    service = IntegrationService(session)
    return await service.get_board_columns(provider=provider, payload=payload)


@router.get(
    "/{provider}",
    response_model=JiraIntegrationConfigPublic,
    summary="Get integration config by project ID",
)
def get_integration_config_by_project(
        project_id: str,
        provider: str,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    service = IntegrationService(session)
    return service.get_config_by_project(provider=provider, project_id=project_id)


@router.get(
    "/{provider}/{config_id}",
    response_model=JiraIntegrationConfigPublic,
    summary="Get integration config by ID",
)
def get_integration_config_by_id(
        project_id: str,
        provider: str,
        config_id: str,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    service = IntegrationService(session)
    config = service.get_config_by_id(provider=provider, config_id=config_id)
    return config


@router.post(
    "/{provider}",
    response_model=JiraIntegrationConfigPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Create integration config for a provider",
)
async def create_integration_config(
        project_id: str,
        provider: str,
        payload: JiraIntegrationConfigCreate,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
        test_connection: Annotated[
            bool,
            Query(description="Testar conexão com o provedor antes de salvar"),
        ] = False,
):
    service = IntegrationService(session)
    return await service.create_config(
        provider=provider, payload=payload, test_conn=test_connection
    )


@router.put(
    "/{provider}/{config_id}",
    response_model=JiraIntegrationConfigPublic,
    summary="Update integration config for a provider",
)
async def update_integration_config(
        project_id: str,
        provider: str,
        config_id: str,
        payload: JiraIntegrationConfigUpdate,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
        test_connection: Annotated[
            bool,
            Query(description="Testar conexão com o provedor antes de salvar"),
        ] = False,
):
    service = IntegrationService(session)
    return await service.update_config(
        provider=provider,
        config_id=config_id,
        payload=payload,
        test_conn=test_connection,
    )


@router.delete(
    "/{provider}/{config_id}",
    response_model=dict[str, Any],
    summary="Delete integration config for a provider",
)
def delete_integration_config(
        project_id: str,
        provider: str,
        config_id: str,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    service = IntegrationService(session)
    return service.delete_config(provider=provider, config_id=config_id)
