import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from database.models import User
from utils.config import settings


def test_local_login_success(client: TestClient, db_session: Session, monkeypatch):
    """
    When ESTACK_ENVIRONMENT is 'local' and ESTACK_AUTH_BYPASS_LOCAL is True,
    POST /users/local-login must succeed and return a valid JWT bearer token for admin@elumini-it.com.
    """
    admin_user = User(
        id=str(uuid.uuid4()),
        email="admin@elumini-it.com",
        is_active=True,
        is_admin=True,
    )
    db_session.add(admin_user)
    db_session.commit()

    monkeypatch.setattr(settings, "estack_environment", "local")
    monkeypatch.setattr(settings, "estack_auth_bypass_local", True)

    response = client.post("/users/local-login")
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"

    monkeypatch.setattr(
        "utils.bearer_auth.oauth2_authenticator.verify_token",
        lambda token: admin_user,
    )
    token = data["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    users_response = client.get("/users/token-usage", headers=headers)
    assert users_response.status_code == 200


def test_local_login_fallback_to_any_admin(client: TestClient, db_session: Session, monkeypatch):
    """
    If seed_admin_email is not configured or not matched, it falls back to an existing admin.
    """
    admin_user = User(
        id=str(uuid.uuid4()),
        email="fallback_admin@test.com",
        is_active=True,
        is_admin=True,
    )
    db_session.add(admin_user)
    db_session.commit()

    monkeypatch.setattr(settings, "estack_environment", "local")
    monkeypatch.setattr(settings, "estack_auth_bypass_local", True)
    monkeypatch.setattr(settings, "seed_admin_email", None)
    monkeypatch.setattr(settings, "email_user", "nonexistent@test.com")

    response = client.post("/users/local-login")
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"

    monkeypatch.setattr(
        "utils.bearer_auth.oauth2_authenticator.verify_token",
        lambda token: admin_user,
    )
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    users_response = client.get("/users/token-usage", headers=headers)
    assert users_response.status_code == 200


def test_local_login_fails_when_environment_is_production(
    client: TestClient, db_session: Session, monkeypatch
):
    """
    Fail-safe: Even if ESTACK_AUTH_BYPASS_LOCAL is True, if ESTACK_ENVIRONMENT is 'production',
    the endpoint must return 403 Forbidden.
    """
    admin_user = User(
        id=str(uuid.uuid4()),
        email="admin@production.com",
        is_active=True,
        is_admin=True,
    )
    db_session.add(admin_user)
    db_session.commit()

    monkeypatch.setattr(settings, "estack_environment", "production")
    monkeypatch.setattr(settings, "estack_auth_bypass_local", True)

    response = client.post("/users/local-login")
    assert response.status_code == 403
    assert response.json()["detail"] == "Local login is disabled in this environment"


def test_local_login_fails_when_environment_is_staging(
    client: TestClient, db_session: Session, monkeypatch
):
    """
    Fail-safe: If ESTACK_ENVIRONMENT is 'staging', return 403 Forbidden.
    """
    monkeypatch.setattr(settings, "estack_environment", "staging")
    monkeypatch.setattr(settings, "estack_auth_bypass_local", True)

    response = client.post("/users/local-login")
    assert response.status_code == 403
    assert response.json()["detail"] == "Local login is disabled in this environment"


def test_local_login_fails_when_flag_is_false(
    client: TestClient, db_session: Session, monkeypatch
):
    """
    Fail-safe: If ESTACK_ENVIRONMENT is 'local' but ESTACK_AUTH_BYPASS_LOCAL is False,
    the endpoint must return 403 Forbidden.
    """
    monkeypatch.setattr(settings, "estack_environment", "local")
    monkeypatch.setattr(settings, "estack_auth_bypass_local", False)

    response = client.post("/users/local-login")
    assert response.status_code == 403
    assert response.json()["detail"] == "Local login is disabled in this environment"


def test_local_login_fails_when_no_admin_exists(
    client: TestClient, db_session: Session, monkeypatch
):
    """
    If local auth is enabled but no admin user exists in DB, return 404.
    """
    monkeypatch.setattr(settings, "estack_environment", "local")
    monkeypatch.setattr(settings, "estack_auth_bypass_local", True)
    monkeypatch.setattr(settings, "seed_admin_email", "missing_admin@test.com")

    response = client.post("/users/local-login")
    assert response.status_code == 404
    assert "Default admin user not found" in response.json()["detail"]
