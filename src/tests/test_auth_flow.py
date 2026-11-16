from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from database.models import User, UserAccessCode
from schemas.user_schemas import Token


class TestGenerateAccessCode:
    def test_generate_code_for_existing_user(
        self, client: TestClient, sample_user: User
    ):
        mock_auth = MagicMock()
        mock_auth.store_and_send_code = MagicMock()
        with patch(
            "src.routers.crud.user_router.oauth2_authenticator", mock_auth
        ):
            response = client.post(
                "/users/generate-access-code",
                json={"email": sample_user.email},
            )
            assert response.status_code == 200
            mock_auth.store_and_send_code.assert_called_once_with(
                email=sample_user.email
            )

    def test_generate_code_for_nonexistent_user(
        self, client: TestClient
    ):
        response = client.post(
            "/users/generate-access-code",
            json={"email": "nobody@example.com"},
        )
        assert response.status_code == 404
        assert response.json()["detail"] == "User not found"


class TestValidateAccessCode:
    def test_validate_code_success(
        self, client: TestClient, sample_user: User
    ):
        fake_token = Token(access_token="fake-jwt-token")
        mock_auth = MagicMock()
        mock_auth.authenticate_user_with_code.return_value = fake_token
        with patch(
            "src.routers.crud.user_router.oauth2_authenticator", mock_auth
        ):
            response = client.post(
                "/users/validate-access-code",
                json={"email": sample_user.email, "code": "1234"},
            )
            assert response.status_code == 200
            data = response.json()
            assert data["access_token"] == "fake-jwt-token"
            assert data["token_type"] == "bearer"
            mock_auth.authenticate_user_with_code.assert_called_once_with(
                sample_user.email, "1234"
            )

    def test_validate_code_invalid_returns_401(
        self, client: TestClient, sample_user: User
    ):
        mock_auth = MagicMock()
        mock_auth.authenticate_user_with_code.return_value = None
        with patch(
            "src.routers.crud.user_router.oauth2_authenticator", mock_auth
        ):
            response = client.post(
                "/users/validate-access-code",
                json={"email": sample_user.email, "code": "0000"},
            )
            assert response.status_code == 401
            assert response.json()["detail"] == "Invalid or expired access code"

    def test_validate_code_missing_fields_returns_422(self, client: TestClient):
        response = client.post(
            "/users/validate-access-code",
            json={"email": "test@example.com"},
        )
        assert response.status_code == 422


class TestFullAuthFlow:
    def test_generate_then_validate(
        self, client: TestClient, sample_user: User
    ):
        mock_gen = MagicMock()
        mock_gen.store_and_send_code = MagicMock()
        with patch(
            "src.routers.crud.user_router.oauth2_authenticator", mock_gen
        ):
            response = client.post(
                "/users/generate-access-code",
                json={"email": sample_user.email},
            )
            assert response.status_code == 200

        fake_token = Token(access_token="session-jwt")
        mock_val = MagicMock()
        mock_val.authenticate_user_with_code.return_value = fake_token
        with patch(
            "src.routers.crud.user_router.oauth2_authenticator", mock_val
        ):
            response = client.post(
                "/users/validate-access-code",
                json={"email": sample_user.email, "code": "1234"},
            )
            assert response.status_code == 200
            assert response.json()["access_token"] == "session-jwt"


class TestAccessCodeFixture:
    def test_user_access_code_created(
        self, sample_user_access_code: UserAccessCode
    ):
        assert sample_user_access_code.access_code == "1234"
        assert sample_user_access_code.user_email is not None
