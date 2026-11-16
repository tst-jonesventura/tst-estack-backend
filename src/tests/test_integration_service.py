from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from database.models import (
    JiraIntegrationColumnsMapping,
    JiraIntegrationConfig,
    Project,
)
from integrations.jira_provider import JiraIntegrationProvider
from schemas.integration_schemas import (
    JiraGetColumnsPayload,
    JiraIntegrationColumnsMappingCreate,
    JiraIntegrationConfigCreate,
    JiraIntegrationConfigUpdate,
    JiraTestConnectionPayload,
)
from services.integration_service import IntegrationService


@pytest.fixture
def mock_session():
    session = MagicMock(spec=Session)
    return session


@pytest.fixture
def sample_jira_create():
    return JiraIntegrationConfigCreate(
        project_id="proj-123",
        jira_base_url="https://test.atlassian.net",
        jira_email="user@test.com",
        jira_api_token="secret-token",
        jira_project_id="10000",
        jira_project_key="TEST",
        default_issue_type="Story",
        column_mappings=[
            JiraIntegrationColumnsMappingCreate(
                project_kanban_column_id="col-1",
                jira_status_id="10001",
                jira_status_name="To Do",
            )
        ],
    )


@pytest.fixture
def sample_jira_config():
    config = JiraIntegrationConfig(
        id="config-123",
        project_id="proj-123",
        jira_base_url="https://test.atlassian.net",
        jira_email="user@test.com",
        jira_api_token="secret-token",
        jira_project_id="10000",
        jira_project_key="TEST",
        default_issue_type="Story",
    )
    config.column_mappings = [
        JiraIntegrationColumnsMapping(
            id="cm-1",
            jira_integration_config_id="config-123",
            project_kanban_column_id="col-1",
            jira_status_id="10001",
            jira_status_name="To Do",
        )
    ]
    return config


def test_validate_provider_unsupported(mock_session):
    service = IntegrationService(mock_session)
    with pytest.raises(HTTPException) as exc:
        service._validate_provider("unknown_provider")
    assert exc.value.status_code == 400
    assert "not supported" in exc.value.detail


def test_validate_provider_future_not_implemented(mock_session):
    service = IntegrationService(mock_session)
    for prov in ["azure", "trello"]:
        with pytest.raises(HTTPException) as exc:
            service._validate_provider(prov)
        assert exc.value.status_code == 501
        assert "not yet implemented" in exc.value.detail


def test_get_provider_instance_jira(mock_session):
    service = IntegrationService(mock_session)
    provider = service.get_provider_instance("jira")
    assert isinstance(provider, JiraIntegrationProvider)


def test_get_provider_instance_unimplemented(mock_session):
    service = IntegrationService(mock_session)
    with patch.object(service, "_validate_provider", return_value="other"):
        with pytest.raises(HTTPException) as exc:
            service.get_provider_instance("other")
        assert exc.value.status_code == 501


def test_list_configs_without_project_id(mock_session, sample_jira_config):
    mock_session.query.return_value.options.return_value.all.return_value = [sample_jira_config]
    service = IntegrationService(mock_session)
    configs = service.list_configs("jira")
    assert len(configs) == 1
    assert configs[0].id == "config-123"


def test_list_configs_with_project_id(mock_session, sample_jira_config):
    mock_session.query.return_value.options.return_value.filter.return_value.all.return_value = [sample_jira_config]
    service = IntegrationService(mock_session)
    configs = service.list_configs("jira", project_id="proj-123")
    assert len(configs) == 1
    assert configs[0].project_id == "proj-123"


def test_list_configs_non_jira(mock_session):
    service = IntegrationService(mock_session)
    with patch.object(service, "_validate_provider", return_value="other"):
        configs = service.list_configs("other")
        assert configs == []


def test_get_config_by_id_success(mock_session, sample_jira_config):
    mock_session.query.return_value.options.return_value.filter.return_value.first.return_value = sample_jira_config
    service = IntegrationService(mock_session)
    config = service.get_config_by_id("jira", "config-123")
    assert config.id == "config-123"


def test_get_config_by_id_not_found(mock_session):
    mock_session.query.return_value.options.return_value.filter.return_value.first.return_value = None
    service = IntegrationService(mock_session)
    with pytest.raises(HTTPException) as exc:
        service.get_config_by_id("jira", "nonexistent")
    assert exc.value.status_code == 404


def test_get_config_by_id_non_jira(mock_session):
    service = IntegrationService(mock_session)
    with patch.object(service, "_validate_provider", return_value="other"):
        config = service.get_config_by_id("other", "config-123")
        assert config is None


def test_get_config_by_project_success(mock_session, sample_jira_config):
    mock_session.query.return_value.options.return_value.filter.return_value.first.return_value = sample_jira_config
    service = IntegrationService(mock_session)
    config = service.get_config_by_project("jira", "proj-123")
    assert config.project_id == "proj-123"


def test_get_config_by_project_not_found(mock_session):
    mock_session.query.return_value.options.return_value.filter.return_value.first.return_value = None
    service = IntegrationService(mock_session)
    with pytest.raises(HTTPException) as exc:
        service.get_config_by_project("jira", "proj-unknown")
    assert exc.value.status_code == 404


def test_get_config_by_project_non_jira(mock_session):
    service = IntegrationService(mock_session)
    with patch.object(service, "_validate_provider", return_value="other"):
        config = service.get_config_by_project("other", "proj-123")
        assert config is None


@pytest.mark.anyio
async def test_create_config_success(mock_session, sample_jira_create):
    mock_project = Project(id="proj-123", name="Test Project")
    mock_session.query.return_value.filter.side_effect = [
        MagicMock(first=MagicMock(return_value=mock_project)),  # Project query
        MagicMock(first=MagicMock(return_value=None)),          # Existing config query
    ]

    service = IntegrationService(mock_session)
    config = await service.create_config("jira", sample_jira_create)

    assert config.project_id == "proj-123"
    assert config.jira_project_key == "TEST"
    assert mock_session.add.call_count >= 2  # config + 1 column mapping
    mock_session.commit.assert_called_once()


@pytest.mark.anyio
async def test_create_config_without_column_mappings(mock_session):
    sample_create = JiraIntegrationConfigCreate(
        project_id="proj-123",
        jira_base_url="https://test.atlassian.net",
        jira_email="user@test.com",
        jira_api_token="secret-token",
        jira_project_id="10000",
        jira_project_key="TEST",
        default_issue_type="Story",
        column_mappings=None,
    )
    mock_project = Project(id="proj-123", name="Test Project")
    mock_session.query.return_value.filter.side_effect = [
        MagicMock(first=MagicMock(return_value=mock_project)),
        MagicMock(first=MagicMock(return_value=None)),
    ]

    service = IntegrationService(mock_session)
    config = await service.create_config("jira", sample_create)
    assert config.project_id == "proj-123"
    mock_session.commit.assert_called_once()


@pytest.mark.anyio
async def test_create_config_project_not_found(mock_session, sample_jira_create):
    mock_session.query.return_value.filter.return_value.first.return_value = None
    service = IntegrationService(mock_session)
    with pytest.raises(HTTPException) as exc:
        await service.create_config("jira", sample_jira_create)
    assert exc.value.status_code == 404
    assert "Project 'proj-123' not found" in exc.value.detail


@pytest.mark.anyio
async def test_create_config_already_exists(mock_session, sample_jira_create, sample_jira_config):
    mock_project = Project(id="proj-123", name="Test Project")
    mock_session.query.return_value.filter.side_effect = [
        MagicMock(first=MagicMock(return_value=mock_project)),      # Project query
        MagicMock(first=MagicMock(return_value=sample_jira_config)),# Existing config
    ]
    service = IntegrationService(mock_session)
    with pytest.raises(HTTPException) as exc:
        await service.create_config("jira", sample_jira_create)
    assert exc.value.status_code == 400
    assert "already exists" in exc.value.detail


@pytest.mark.anyio
async def test_create_config_with_test_conn(mock_session, sample_jira_create):
    mock_project = Project(id="proj-123", name="Test Project")
    mock_session.query.return_value.filter.side_effect = [
        MagicMock(first=MagicMock(return_value=mock_project)),
        MagicMock(first=MagicMock(return_value=None)),
    ]

    service = IntegrationService(mock_session)
    with patch.object(service, "get_provider_instance") as mock_get_prov:
        mock_provider = AsyncMock()
        mock_provider.test_connection.return_value = True
        mock_get_prov.return_value = mock_provider

        config = await service.create_config("jira", sample_jira_create, test_conn=True)
        assert config.project_id == "proj-123"
        mock_provider.test_connection.assert_called_once()


@pytest.mark.anyio
async def test_create_config_with_test_conn_failed(mock_session, sample_jira_create):
    mock_project = Project(id="proj-123", name="Test Project")
    mock_session.query.return_value.filter.side_effect = [
        MagicMock(first=MagicMock(return_value=mock_project)),
        MagicMock(first=MagicMock(return_value=None)),
    ]

    service = IntegrationService(mock_session)
    with patch.object(service, "get_provider_instance") as mock_get_prov:
        mock_provider = AsyncMock()
        mock_provider.test_connection.side_effect = Exception("401 Unauthorized")
        mock_get_prov.return_value = mock_provider

        with pytest.raises(HTTPException) as exc:
            await service.create_config("jira", sample_jira_create, test_conn=True)
        assert exc.value.status_code == 400
        assert "Failed to connect to Jira" in exc.value.detail


@pytest.mark.anyio
async def test_create_config_non_jira(mock_session, sample_jira_create):
    service = IntegrationService(mock_session)
    with patch.object(service, "_validate_provider", return_value="other"):
        config = await service.create_config("other", sample_jira_create)
        assert config is None


@pytest.mark.anyio
async def test_update_config_success(mock_session, sample_jira_config):
    mock_session.query.return_value.options.return_value.filter.return_value.first.return_value = sample_jira_config
    update_payload = JiraIntegrationConfigUpdate(
        jira_base_url="https://new.atlassian.net/",
        jira_project_key="NEW_KEY",
        column_mappings=[
            JiraIntegrationColumnsMappingCreate(
                project_kanban_column_id="col-2",
                jira_status_id="10002",
                jira_status_name="In Progress",
            )
        ],
    )

    service = IntegrationService(mock_session)
    updated = await service.update_config("jira", "config-123", update_payload)

    assert updated.jira_project_key == "NEW_KEY"
    assert updated.jira_base_url == "https://new.atlassian.net"
    mock_session.commit.assert_called_once()


@pytest.mark.anyio
async def test_update_config_without_column_mappings(mock_session, sample_jira_config):
    mock_session.query.return_value.options.return_value.filter.return_value.first.return_value = sample_jira_config
    update_payload = JiraIntegrationConfigUpdate(
        jira_project_key="NEW_KEY",
        column_mappings=None,
    )

    service = IntegrationService(mock_session)
    updated = await service.update_config("jira", "config-123", update_payload)

    assert updated.jira_project_key == "NEW_KEY"
    mock_session.commit.assert_called_once()


@pytest.mark.anyio
async def test_update_config_not_found(mock_session):
    mock_session.query.return_value.options.return_value.filter.return_value.first.return_value = None
    service = IntegrationService(mock_session)
    update_payload = JiraIntegrationConfigUpdate(jira_project_key="NEW_KEY")
    with pytest.raises(HTTPException) as exc:
        await service.update_config("jira", "config-nonexistent", update_payload)
    assert exc.value.status_code == 404
    assert "not found" in exc.value.detail


@pytest.mark.anyio
async def test_update_config_with_test_conn(mock_session, sample_jira_config):
    mock_session.query.return_value.options.return_value.filter.return_value.first.return_value = sample_jira_config
    update_payload = JiraIntegrationConfigUpdate(jira_project_key="NEW_KEY")

    service = IntegrationService(mock_session)
    with patch.object(service, "get_provider_instance") as mock_get_prov:
        mock_provider = AsyncMock()
        mock_provider.test_connection.return_value = True
        mock_get_prov.return_value = mock_provider

        updated = await service.update_config("jira", "config-123", update_payload, test_conn=True)
        assert updated.jira_project_key == "NEW_KEY"
        mock_provider.test_connection.assert_called_once_with(sample_jira_config)


@pytest.mark.anyio
async def test_update_config_with_test_conn_failed(mock_session, sample_jira_config):
    mock_session.query.return_value.options.return_value.filter.return_value.first.return_value = sample_jira_config
    update_payload = JiraIntegrationConfigUpdate(jira_project_key="NEW_KEY")

    service = IntegrationService(mock_session)
    with patch.object(service, "get_provider_instance") as mock_get_prov:
        mock_provider = AsyncMock()
        mock_provider.test_connection.side_effect = Exception("Auth failed")
        mock_get_prov.return_value = mock_provider

        with pytest.raises(HTTPException) as exc:
            await service.update_config("jira", "config-123", update_payload, test_conn=True)
        assert exc.value.status_code == 400
        assert "Failed to connect to Jira" in exc.value.detail


@pytest.mark.anyio
async def test_update_config_non_jira(mock_session):
    service = IntegrationService(mock_session)
    update_payload = JiraIntegrationConfigUpdate(jira_project_key="NEW_KEY")
    with patch.object(service, "_validate_provider", return_value="other"):
        config = await service.update_config("other", "config-123", update_payload)
        assert config is None


def test_delete_config_success(mock_session, sample_jira_config):
    mock_session.query.return_value.filter.return_value.first.return_value = sample_jira_config
    service = IntegrationService(mock_session)
    res = service.delete_config("jira", "config-123")
    assert "deleted successfully" in res["detail"]
    mock_session.delete.assert_called_once_with(sample_jira_config)
    mock_session.commit.assert_called_once()


def test_delete_config_not_found(mock_session):
    mock_session.query.return_value.filter.return_value.first.return_value = None
    service = IntegrationService(mock_session)
    with pytest.raises(HTTPException) as exc:
        service.delete_config("jira", "config-nonexistent")
    assert exc.value.status_code == 404
    assert "not found" in exc.value.detail


def test_delete_config_non_jira(mock_session):
    service = IntegrationService(mock_session)
    with patch.object(service, "_validate_provider", return_value="other"):
        res = service.delete_config("other", "config-123")
        assert res == {}


@pytest.mark.anyio
async def test_test_connection_and_get_projects_success(mock_session):
    service = IntegrationService(mock_session)
    payload = JiraTestConnectionPayload(
        basepath="https://test.atlassian.net",
        email="test@example.com",
        token="secret",
    )
    with patch.object(service, "get_provider_instance") as mock_get_prov:
        mock_provider = AsyncMock()
        mock_provider.get_projects.return_value = [
            {"id": "10000", "key": "ESTACK", "name": "E-Stack"}
        ]
        mock_get_prov.return_value = mock_provider

        projects = await service.test_connection_and_get_projects("jira", payload)
        assert len(projects) == 1
        assert projects[0]["key"] == "ESTACK"
        mock_provider.get_projects.assert_called_once_with(
            "https://test.atlassian.net", "test@example.com", "secret"
        )


@pytest.mark.anyio
async def test_test_connection_and_get_projects_missing_fields(mock_session):
    service = IntegrationService(mock_session)
    payload = JiraTestConnectionPayload(basepath="", email="", token="")
    with pytest.raises(HTTPException) as exc:
        await service.test_connection_and_get_projects("jira", payload)
    assert exc.value.status_code == 400


@pytest.mark.anyio
async def test_test_connection_and_get_projects_http_exception(mock_session):
    service = IntegrationService(mock_session)
    payload = JiraTestConnectionPayload(
        basepath="https://test.atlassian.net",
        email="test@example.com",
        token="secret",
    )
    with patch.object(service, "get_provider_instance") as mock_get_prov:
        mock_provider = AsyncMock()
        mock_provider.get_projects.side_effect = HTTPException(status_code=403, detail="Forbidden")
        mock_get_prov.return_value = mock_provider

        with pytest.raises(HTTPException) as exc:
            await service.test_connection_and_get_projects("jira", payload)
        assert exc.value.status_code == 403


@pytest.mark.anyio
async def test_test_connection_and_get_projects_http_status_error(mock_session):
    service = IntegrationService(mock_session)
    payload = JiraTestConnectionPayload(
        basepath="https://test.atlassian.net",
        email="test@example.com",
        token="secret",
    )
    with patch.object(service, "get_provider_instance") as mock_get_prov:
        mock_provider = AsyncMock()
        request = httpx.Request("GET", "https://test.atlassian.net")
        response = httpx.Response(401, text="Unauthorized access", request=request)
        mock_provider.get_projects.side_effect = httpx.HTTPStatusError("Error", request=request, response=response)
        mock_get_prov.return_value = mock_provider

        with pytest.raises(HTTPException) as exc:
            await service.test_connection_and_get_projects("jira", payload)
        assert exc.value.status_code == 401
        assert "Unauthorized access" in exc.value.detail


@pytest.mark.anyio
async def test_test_connection_and_get_projects_general_exception(mock_session):
    service = IntegrationService(mock_session)
    payload = JiraTestConnectionPayload(
        basepath="https://test.atlassian.net",
        email="test@example.com",
        token="secret",
    )
    with patch.object(service, "get_provider_instance") as mock_get_prov:
        mock_provider = AsyncMock()
        mock_provider.get_projects.side_effect = Exception("Connection timeout")
        mock_get_prov.return_value = mock_provider

        with pytest.raises(HTTPException) as exc:
            await service.test_connection_and_get_projects("jira", payload)
        assert exc.value.status_code == 400
        assert "Failed to connect to Jira" in exc.value.detail


@pytest.mark.anyio
async def test_get_board_columns_success(mock_session):
    service = IntegrationService(mock_session)
    payload = JiraGetColumnsPayload(
        basepath="https://test.atlassian.net",
        email="test@example.com",
        token="secret",
        board_id=1,
    )
    with patch.object(service, "get_provider_instance") as mock_get_prov:
        mock_provider = AsyncMock()
        mock_provider.get_board_columns.return_value = {
            "columnConfig": {"columns": [{"name": "To Do"}]}
        }
        mock_get_prov.return_value = mock_provider

        result = await service.get_board_columns("jira", payload)
        assert "columnConfig" in result
        mock_provider.get_board_columns.assert_called_once_with(
            base_url="https://test.atlassian.net",
            email="test@example.com",
            token="secret",
            board_id=1,
            project_key_or_id=None,
        )


@pytest.mark.anyio
async def test_get_board_columns_from_db_credentials_success(mock_session, sample_jira_config):
    service = IntegrationService(mock_session)
    payload = JiraGetColumnsPayload(
        project_id="proj-123",
        project_key="TEST",
        board_id=1,
    )
    with patch.object(service, "get_config_by_project", return_value=sample_jira_config), \
         patch.object(service, "get_provider_instance") as mock_get_prov:
        mock_provider = AsyncMock()
        mock_provider.get_board_columns.return_value = {"columns": []}
        mock_get_prov.return_value = mock_provider

        result = await service.get_board_columns("jira", payload)
        assert result == {"columns": []}
        mock_provider.get_board_columns.assert_called_once_with(
            base_url="https://test.atlassian.net",
            email="user@test.com",
            token="secret-token",
            board_id=1,
            project_key_or_id="TEST",
        )


@pytest.mark.anyio
async def test_get_board_columns_from_db_credentials_without_project_key(mock_session, sample_jira_config):
    service = IntegrationService(mock_session)
    payload = JiraGetColumnsPayload(
        project_id="proj-123",
        board_id=1,
    )
    with patch.object(service, "get_config_by_project", return_value=sample_jira_config), \
         patch.object(service, "get_provider_instance") as mock_get_prov:
        mock_provider = AsyncMock()
        mock_provider.get_board_columns.return_value = {"columns": []}
        mock_get_prov.return_value = mock_provider

        result = await service.get_board_columns("jira", payload)
        assert result == {"columns": []}
        mock_provider.get_board_columns.assert_called_once_with(
            base_url="https://test.atlassian.net",
            email="user@test.com",
            token="secret-token",
            board_id=1,
            project_key_or_id="proj-123",
        )


def test_load_credentials_from_db_with_empty_project_key(mock_session, sample_jira_config):
    service = IntegrationService(mock_session)
    with patch.object(service, "get_config_by_project", return_value=sample_jira_config):
        base_url, email, token, project_key = service._load_credentials_from_db("jira", "proj-123", None)
        assert base_url == "https://test.atlassian.net"
        assert email == "user@test.com"
        assert token == "secret-token"
        assert project_key == "TEST"


def test_load_credentials_from_db_with_jira_project_id_fallback(mock_session, sample_jira_config):
    sample_jira_config.jira_project_key = None
    sample_jira_config.jira_project_id = "10000"
    service = IntegrationService(mock_session)
    with patch.object(service, "get_config_by_project", return_value=sample_jira_config):
        base_url, email, token, project_key = service._load_credentials_from_db("jira", "proj-123", None)
        assert project_key == "10000"


@pytest.mark.anyio
async def test_get_board_columns_from_db_credentials_db_not_found(mock_session):
    service = IntegrationService(mock_session)
    payload = JiraGetColumnsPayload(
        project_id="proj-123",
    )
    with patch.object(service, "get_config_by_project", side_effect=HTTPException(status_code=404, detail="Not found")):
        with pytest.raises(HTTPException) as exc:
            await service.get_board_columns("jira", payload)
        assert exc.value.status_code == 400
        assert "basepath (or jira_base_url)" in exc.value.detail


@pytest.mark.anyio
async def test_get_board_columns_missing_params(mock_session):
    service = IntegrationService(mock_session)
    payload = JiraGetColumnsPayload(
        basepath="https://test.atlassian.net",
        email="test@example.com",
        token="secret",
    )
    with pytest.raises(HTTPException) as exc:
        await service.get_board_columns("jira", payload)
    assert exc.value.status_code == 400
    assert "Either board_id, project_key, or project_id is required" in exc.value.detail


@pytest.mark.anyio
async def test_get_board_columns_http_exception(mock_session):
    service = IntegrationService(mock_session)
    payload = JiraGetColumnsPayload(
        basepath="https://test.atlassian.net",
        email="test@example.com",
        token="secret",
        board_id=1,
    )
    with patch.object(service, "get_provider_instance") as mock_get_prov:
        mock_provider = AsyncMock()
        mock_provider.get_board_columns.side_effect = HTTPException(status_code=403, detail="Forbidden")
        mock_get_prov.return_value = mock_provider

        with pytest.raises(HTTPException) as exc:
            await service.get_board_columns("jira", payload)
        assert exc.value.status_code == 403


@pytest.mark.anyio
async def test_get_board_columns_http_status_error(mock_session):
    service = IntegrationService(mock_session)
    payload = JiraGetColumnsPayload(
        basepath="https://test.atlassian.net",
        email="test@example.com",
        token="secret",
        board_id=1,
    )
    with patch.object(service, "get_provider_instance") as mock_get_prov:
        mock_provider = AsyncMock()
        request = httpx.Request("GET", "https://test.atlassian.net")
        response = httpx.Response(404, text="Board not found", request=request)
        mock_provider.get_board_columns.side_effect = httpx.HTTPStatusError("Not Found", request=request, response=response)
        mock_get_prov.return_value = mock_provider

        with pytest.raises(HTTPException) as exc:
            await service.get_board_columns("jira", payload)
        assert exc.value.status_code == 404
        assert "Board not found" in exc.value.detail


@pytest.mark.anyio
async def test_get_board_columns_value_error(mock_session):
    service = IntegrationService(mock_session)
    payload = JiraGetColumnsPayload(
        basepath="https://test.atlassian.net",
        email="test@example.com",
        token="secret",
        board_id=1,
    )
    with patch.object(service, "get_provider_instance") as mock_get_prov:
        mock_provider = AsyncMock()
        mock_provider.get_board_columns.side_effect = ValueError("Board not found with given id")
        mock_get_prov.return_value = mock_provider

        with pytest.raises(HTTPException) as exc:
            await service.get_board_columns("jira", payload)
        assert exc.value.status_code == 404
        assert "Board not found with given id" in exc.value.detail


@pytest.mark.anyio
async def test_get_board_columns_general_exception(mock_session):
    service = IntegrationService(mock_session)
    payload = JiraGetColumnsPayload(
        basepath="https://test.atlassian.net",
        email="test@example.com",
        token="secret",
        board_id=1,
    )
    with patch.object(service, "get_provider_instance") as mock_get_prov:
        mock_provider = AsyncMock()
        mock_provider.get_board_columns.side_effect = Exception("Unknown network error")
        mock_get_prov.return_value = mock_provider

        with pytest.raises(HTTPException) as exc:
            await service.get_board_columns("jira", payload)
        assert exc.value.status_code == 400
        assert "Failed to fetch board columns" in exc.value.detail
