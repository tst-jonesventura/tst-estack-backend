from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from database.database import get_session
from routers.webhooks.jira_router import router

# Create a FastAPI app for testing
app = FastAPI()
app.include_router(router)


# Mock dependency
def override_get_session():
    yield MagicMock(spec=Session)


app.dependency_overrides[get_session] = override_get_session

client = TestClient(app)


@pytest.fixture
def mock_sync_service():
    with patch("routers.webhooks.jira_router.SyncService", autospec=True) as mock:
        yield mock


def test_jira_webhook_success(mock_sync_service):
    # Setup
    mock_instance = mock_sync_service.return_value
    mock_instance.handle_webhook = MagicMock()

    payload = {"issue": {"key": "PROJ-123"}}

    # Execute
    response = client.post("/", json=payload)

    # Assert
    assert response.status_code == 200
    assert response.json() == {"detail": "Webhook received"}
    mock_instance.handle_webhook.assert_called_once_with(payload)


def test_jira_webhook_empty_body():
    # Execute
    # FastAPI/TestClient might behave differently with empty body vs empty string
    # The code checks: if not body_bytes:
    response = client.post("/", content="")

    # Assert
    assert response.status_code == 200
    assert response.json() == {"detail": "Empty webhook body"}


def test_jira_webhook_invalid_json():
    # Execute
    response = client.post("/", content="invalid json", headers={"Content-Type": "application/json"})

    # Assert
    assert response.status_code == 200
    assert response.json() == {"detail": "Invalid JSON"}


def test_jira_webhook_exception(mock_sync_service):
    # Setup
    mock_instance = mock_sync_service.return_value
    mock_instance.handle_webhook = MagicMock(side_effect=Exception("Something went wrong"))

    payload = {"issue": {"key": "PROJ-123"}}

    # Execute
    response = client.post("/", json=payload)

    # Assert
    assert response.status_code == 200
    assert response.json() == {"detail": "Error processing webhook"}
