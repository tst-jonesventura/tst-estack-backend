from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from database.database import get_session
from database.models import JiraIntegrationConfig, User
from routers.crud.integration_router import router
from utils.bearer_auth import check_project_access, retrieve_user_data

app = FastAPI()
app.include_router(router, prefix="/projects/{project_id}/integrations")

mock_db_session = MagicMock(spec=Session)
fake_user = User(id="user-123", email="test@example.com", is_active=True, is_admin=False)


def override_get_session():
    yield mock_db_session


def override_retrieve_user_data():
    return fake_user


def override_check_project_access():
    return fake_user


app.dependency_overrides[get_session] = override_get_session
app.dependency_overrides[retrieve_user_data] = override_retrieve_user_data
app.dependency_overrides[check_project_access] = override_check_project_access

client = TestClient(app)


@pytest.fixture
def sample_config_model():
    config = JiraIntegrationConfig(
        id="config-123",
        project_id="proj-123",
        jira_base_url="https://test.atlassian.net",
        jira_email="test@example.com",
        jira_api_token="token",
        jira_project_id="10000",
        jira_project_key="TEST",
        default_issue_type="Story",
    )
    config.column_mappings = []
    return config


@patch("routers.crud.integration_router.IntegrationService")
def test_get_integration_by_id_endpoint(mock_service_class, sample_config_model):
    mock_instance = mock_service_class.return_value
    mock_instance.get_config_by_id.return_value = sample_config_model

    response = client.get("/projects/proj-123/integrations/jira/config-123")
    assert response.status_code == 200
    assert response.json()["id"] == "config-123"


@patch("routers.crud.integration_router.IntegrationService")
def test_get_integration_by_project_endpoint(mock_service_class, sample_config_model):
    mock_instance = mock_service_class.return_value
    mock_instance.get_config_by_project.return_value = sample_config_model

    response = client.get("/projects/proj-123/integrations/jira")
    assert response.status_code == 200
    assert response.json()["project_id"] == "proj-123"


@patch("routers.crud.integration_router.IntegrationService")
def test_create_integration_endpoint(mock_service_class, sample_config_model):
    mock_instance = mock_service_class.return_value
    mock_instance.create_config = AsyncMock(return_value=sample_config_model)

    payload = {
        "project_id": "proj-123",
        "jira_base_url": "https://test.atlassian.net",
        "jira_email": "test@example.com",
        "jira_api_token": "token",
        "jira_project_id": "10000",
        "jira_project_key": "TEST",
        "default_issue_type": "Story",
        "column_mappings": [],
    }

    response = client.post("/projects/proj-123/integrations/jira", json=payload)
    assert response.status_code == 201
    assert response.json()["id"] == "config-123"


@patch("routers.crud.integration_router.IntegrationService")
def test_update_integration_endpoint(mock_service_class, sample_config_model):
    mock_instance = mock_service_class.return_value
    mock_instance.get_config_by_id.return_value = sample_config_model
    mock_instance.update_config = AsyncMock(return_value=sample_config_model)

    payload = {"jira_project_key": "UPDATED"}

    response = client.put("/projects/proj-123/integrations/jira/config-123", json=payload)
    assert response.status_code == 200
    assert response.json()["id"] == "config-123"


@patch("routers.crud.integration_router.IntegrationService")
def test_delete_integration_endpoint(mock_service_class, sample_config_model):
    mock_instance = mock_service_class.return_value
    mock_instance.get_config_by_id.return_value = sample_config_model
    mock_instance.delete_config.return_value = {"detail": "deleted"}

    response = client.delete("/projects/proj-123/integrations/jira/config-123")
    assert response.status_code == 200
    assert response.json() == {"detail": "deleted"}


@patch("routers.crud.integration_router.IntegrationService")
def test_test_connection_and_list_projects_endpoint(mock_service_class):
    mock_instance = mock_service_class.return_value
    mock_instance.test_connection_and_get_projects = AsyncMock(
        return_value=[{"id": "10000", "key": "ESTACK", "name": "E-Stack"}]
    )

    payload = {
        "basepath": "https://test.atlassian.net",
        "email": "test@example.com",
        "token": "secret-token",
    }
    response = client.post("/projects/proj-123/integrations/jira/test-connection", json=payload)
    assert response.status_code == 200
    assert response.json() == [{"id": "10000", "key": "ESTACK", "name": "E-Stack"}]


@patch("routers.crud.integration_router.IntegrationService")
def test_get_board_columns_endpoint(mock_service_class):
    mock_instance = mock_service_class.return_value
    mock_instance.get_board_columns = AsyncMock(
        return_value={"columnConfig": {"columns": [{"name": "To Do"}]}}
    )

    payload = {
        "basepath": "https://test.atlassian.net",
        "email": "test@example.com",
        "token": "secret-token",
        "board_id": 42,
    }
    response = client.post("/projects/proj-123/integrations/jira/columns", json=payload)
    assert response.status_code == 200
    assert response.json() == {"columnConfig": {"columns": [{"name": "To Do"}]}}
