import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from database.models import Agent, AgentRun, DocumentTranscription, Project, Role, User, UserProject, UserStory
from utils.token_usage import SPEC_WITHOUT_STORY_LABEL
from src.main import app
from utils.bearer_auth import ensure_admin, retrieve_admin_data


class TestGetCurrentUser:
    def test_get_me_returns_200(
        self, client: TestClient, authenticated_user: User
    ):
        response = client.get("/users/me")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == authenticated_user.id
        assert data["email"] == authenticated_user.email


class TestListUsers:
    def test_list_users_returns_200(
        self, client: TestClient, authenticated_user: User
    ):
        response = client.get("/users/")
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        emails = [u["email"] for u in data]
        assert authenticated_user.email in emails

    def test_list_users_includes_multiple(
        self,
        client: TestClient,
        authenticated_user: User,
        sample_admin_user: User,
    ):
        response = client.get("/users/")
        assert response.status_code == 200
        data = response.json()
        emails = [u["email"] for u in data]
        assert authenticated_user.email in emails
        assert sample_admin_user.email in emails


class TestGetUser:
    def test_get_user_by_id_returns_200(
        self, client: TestClient, authenticated_user: User
    ):
        response = client.get(f"/users/{authenticated_user.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == authenticated_user.id
        assert data["email"] == authenticated_user.email

    def test_get_user_not_found_returns_404(
        self, client: TestClient, authenticated_user: User
    ):
        response = client.get("/users/nonexistent-id")
        assert response.status_code == 404


class TestCreateUser:
    def test_create_user_returns_200(
        self, client: TestClient, authenticated_user: User
    ):
        payload = {
            "email": "newuser@example.com",
            "is_active": True,
            "is_admin": False,
        }
        response = client.post("/users/", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["email"] == "newuser@example.com"
        assert data["is_active"] is True
        assert data["is_admin"] is False
        assert "id" in data

    def test_create_user_missing_email_returns_422(
        self, client: TestClient, authenticated_user: User
    ):
        payload = {"is_active": True}
        response = client.post("/users/", json=payload)
        assert response.status_code == 422

    def test_create_user_admin_flag(
        self, client: TestClient, authenticated_user: User
    ):
        payload = {
            "email": "admin-new@example.com",
            "is_active": True,
            "is_admin": True,
        }
        response = client.post("/users/", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["is_admin"] is True


class TestUpdateUser:
    def test_update_user_returns_200(
        self, client: TestClient, authenticated_user: User
    ):
        payload = {"is_admin": True}
        response = client.put(f"/users/{authenticated_user.id}", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["is_admin"] is True

    def test_update_user_deactivate(
        self, client: TestClient, authenticated_user: User
    ):
        payload = {"is_active": False}
        response = client.put(f"/users/{authenticated_user.id}", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["is_active"] is False

    def test_update_user_not_found_returns_404(
        self, client: TestClient, authenticated_user: User
    ):
        payload = {"is_admin": True}
        response = client.put("/users/nonexistent-id", json=payload)
        assert response.status_code == 404


class TestListUserTokenUsage:
    def test_token_usage_non_admin_returns_403(
        self, client: TestClient, sample_user: User
    ):
        def admin_dependency():
            ensure_admin(sample_user)
            return sample_user

        app.dependency_overrides[retrieve_admin_data] = admin_dependency
        try:
            response = client.get("/users/token-usage")
            assert response.status_code == 403
        finally:
            app.dependency_overrides.pop(retrieve_admin_data, None)

    def test_token_usage_returns_empty_when_no_consumption(
        self,
        client: TestClient,
        sample_admin_user: User,
        sample_user: User,
    ):
        app.dependency_overrides[retrieve_admin_data] = lambda: sample_admin_user
        try:
            response = client.get("/users/token-usage")
            assert response.status_code == 200
            assert response.json() == []
        finally:
            app.dependency_overrides.pop(retrieve_admin_data, None)

    def test_token_usage_empty_when_no_users(
        self,
        client: TestClient,
        sample_admin_user: User,
    ):
        app.dependency_overrides[retrieve_admin_data] = lambda: sample_admin_user
        try:
            with patch("services.project_service.project_service.projects_owned_by_user", return_value=[sample_admin_user]), \
                 patch("services.agent_run_service.agent_run_service.get_usage_rows_filtered_by_projects_ids_grouped_by_user", return_value=[
                     ("ghost-user-id", "gpt-4o", 100),
                 ]), \
                 patch("sqlalchemy.orm.Query.all", return_value=[]):
                response = client.get("/users/token-usage")
                assert response.status_code == 200
                assert response.json() == []
        finally:
            app.dependency_overrides.pop(retrieve_admin_data, None)

    def test_token_usage_skips_user_with_empty_models(
        self,
        client: TestClient,
        sample_admin_user: User,
        sample_user: User,
    ):
        app.dependency_overrides[retrieve_admin_data] = lambda: sample_admin_user
        try:
            # Fake usage rows where tokens are 0 for sample_user, but user exists
            # usage_by_user won't have entries, so models_usage is empty
            with patch("services.project_service.project_service.projects_owned_by_user", return_value=[sample_admin_user]), \
                 patch("services.agent_run_service.agent_run_service.get_usage_rows_filtered_by_projects_ids_grouped_by_user", return_value=[
                     (sample_user.id, "gpt-4o", 0),
                 ]), \
                 patch("sqlalchemy.orm.Query.all", return_value=[sample_user]):
                response = client.get("/users/token-usage")
                assert response.status_code == 200
                assert response.json() == []
        finally:
            app.dependency_overrides.pop(retrieve_admin_data, None)

    def test_token_usage_handles_legacy_3_tuple_and_positive_filter(
        self,
        client: TestClient,
        sample_admin_user: User,
        sample_user: User,
    ):
        app.dependency_overrides[retrieve_admin_data] = lambda: sample_admin_user
        try:
            with patch("services.project_service.project_service.projects_owned_by_user", return_value=[sample_admin_user]), \
                 patch("services.agent_run_service.agent_run_service.get_usage_rows_filtered_by_projects_ids_grouped_by_user", return_value=[
                     (sample_user.id, "gpt-4o", 100),
                     (sample_user.id, "gpt-4o-mini", 0),  # non-positive tokens
                 ]):
                response = client.get("/users/token-usage")
                assert response.status_code == 200
                data = response.json()
                assert len(data) == 1
                assert data[0]["models"][0]["model"] == "gpt-4o"
                assert data[0]["models"][0]["total_tokens"] == 100
        finally:
            app.dependency_overrides.pop(retrieve_admin_data, None)

    def test_token_usage_aggregates_by_model_across_projects(
        self,
        client: TestClient,
        db_session,
        sample_admin_user: User,
        sample_user: User,
        sample_project: Project,
        sample_second_project: Project,
        sample_role: Role,
    ):
        db_session.add(UserProject(
            id=str(uuid.uuid4()),
            user_id=sample_admin_user.id,
            project_id=sample_project.id,
            role_id=sample_role.id,
        ))
        db_session.commit()

        db_session.add_all(
            [
                AgentRun(
                    user_id=sample_user.id,
                    project_id=sample_project.id,
                    title="Run A",
                    status="finished",
                    model="gpt-4o",
                    input_tokens=100,
                    output_tokens=50,
                ),
                AgentRun(
                    user_id=sample_user.id,
                    project_id=sample_second_project.id,
                    title="Run B",
                    status="finished",
                    model="gpt-4o",
                    input_tokens=20,
                    output_tokens=10,
                ),
                AgentRun(
                    user_id=sample_user.id,
                    project_id=sample_project.id,
                    title="Run C",
                    status="finished",
                    model="gpt-4o-mini",
                    input_tokens=5,
                    output_tokens=5,
                ),
            ]
        )
        db_session.commit()

        app.dependency_overrides[retrieve_admin_data] = lambda: sample_admin_user
        try:
            response = client.get("/users/token-usage")
            assert response.status_code == 200
            data = response.json()
            assert len(data) == 1
            user_usage = data[0]
            assert user_usage["id"] == sample_user.id
            assert user_usage["email"] == sample_user.email
            assert user_usage["models"] == [
                {
                    "model": "gpt-4o",
                    "agent_id": None,
                    "agent_name": None,
                    "total_tokens": 180,
                },
                {
                    "model": "gpt-4o-mini",
                    "agent_id": None,
                    "agent_name": None,
                    "total_tokens": 10,
                },
            ]
        finally:
            app.dependency_overrides.pop(retrieve_admin_data, None)


class TestUserTokenUsageDetail:
    def test_detail_non_admin_returns_403(
        self, client: TestClient, sample_user: User, sample_admin_user: User
    ):
        def admin_dependency():
            ensure_admin(sample_user)
            return sample_user

        app.dependency_overrides[retrieve_admin_data] = admin_dependency
        try:
            response = client.get(
                f"/users/{sample_admin_user.id}/token-usage/detail"
            )
            assert response.status_code == 403
        finally:
            app.dependency_overrides.pop(retrieve_admin_data, None)

    def test_detail_ranks_projects_and_specifications(
        self,
        client: TestClient,
        db_session,
        sample_admin_user: User,
        sample_user: User,
        sample_project: Project,
        sample_second_project: Project,
        sample_transcription: DocumentTranscription,
        sample_role: Role,
    ):
        db_session.add(UserProject(
            id=str(uuid.uuid4()),
            user_id=sample_admin_user.id,
            project_id=sample_project.id,
            role_id=sample_role.id,
        ))
        db_session.commit()

        agent = Agent(
            id=str(uuid.uuid4()),
            name="tutor_agent",
            model="openai:gpt-4o",
            is_default=True,
            max_retries=1,
        )
        db_session.add(agent)

        story = UserStory(
            id="story-user-detail-1",
            name="Login Story",
            description="Story for user token detail",
            points=2,
            is_done=False,
            project_id=sample_project.id,
            user_id=sample_user.id,
            transcription_id=sample_transcription.id,
            log="",
        )
        db_session.add(story)
        db_session.add_all(
            [
                AgentRun(
                    user_id=sample_user.id,
                    project_id=sample_project.id,
                    user_story_id=story.id,
                    title="Story run",
                    status="finished",
                    model="gpt-4o",
                    agent_id=agent.id,
                    input_tokens=40,
                    output_tokens=10,
                ),
                AgentRun(
                    user_id=sample_user.id,
                    project_id=sample_second_project.id,
                    user_story_id=None,
                    title="Analysis run",
                    status="finished",
                    model="gpt-4o-mini",
                    agent_id=None,
                    input_tokens=5,
                    output_tokens=5,
                ),
            ]
        )
        db_session.commit()

        app.dependency_overrides[retrieve_admin_data] = lambda: sample_admin_user
        try:
            response = client.get(f"/users/{sample_user.id}/token-usage/detail")
            assert response.status_code == 200
            data = response.json()
            assert data["id"] == sample_user.id
            assert len(data["projects"]) == 2
            assert data["projects"][0]["label"] == sample_project.name
            assert data["projects"][0]["total_tokens"] == 50
            assert data["projects"][1]["label"] == sample_second_project.name
            assert data["projects"][1]["total_tokens"] == 10

            assert len(data["specifications"]) == 2
            assert data["specifications"][0]["label"] == "Login Story"
            assert data["specifications"][0]["total_tokens"] == 50
            assert data["specifications"][1]["label"] == SPEC_WITHOUT_STORY_LABEL
            assert data["specifications"][1]["total_tokens"] == 10

            assert len(data["agents"]) == 1
            assert data["agents"][0]["id"] == agent.id
            assert data["agents"][0]["label"] == "tutor_agent"
            assert data["agents"][0]["total_tokens"] == 50
            assert data["agents"][0]["models"] == [
                {
                    "model": "gpt-4o",
                    "agent_id": agent.id,
                    "agent_name": "tutor_agent",
                    "total_tokens": 50,
                }
            ]
        finally:
            app.dependency_overrides.pop(retrieve_admin_data, None)

    def test_detail_returns_empty_when_no_projects(
        self,
        client: TestClient,
        sample_user: User,
        sample_admin_user: User,
    ):
        app.dependency_overrides[retrieve_admin_data] = lambda: sample_admin_user
        try:
            with patch("services.project_service.project_service.projects_owned_by_user", return_value=[]):
                response = client.get(f"/users/{sample_user.id}/token-usage/detail")
                assert response.status_code == 200
                data = response.json()
                assert data["id"] == sample_user.id
                assert data["email"] == sample_user.email
                assert data["projects"] == []
                assert data["specifications"] == []
                assert data["agents"] == []
        finally:
            app.dependency_overrides.pop(retrieve_admin_data, None)


class TestAccessCode:
    def test_generate_access_code_success(
        self,
        client: TestClient,
        sample_user: User,
    ):
        with patch("utils.oauth2_integration.oauth2_authenticator.store_and_send_code") as mock_send:
            response = client.post("/users/generate-access-code", json={"email": sample_user.email})
            assert response.status_code == 200
            assert mock_send.called

    def test_generate_access_code_user_not_found(
        self,
        client: TestClient,
    ):
        response = client.post("/users/generate-access-code", json={"email": "nonexistent@example.com"})
        assert response.status_code == 404
        assert response.json()["detail"] == "User not found"

    def test_validate_access_code_success(
        self,
        client: TestClient,
        sample_user: User,
    ):
        with patch("utils.oauth2_integration.oauth2_authenticator.authenticate_user_with_code", return_value={"access_token": "fake-token", "token_type": "bearer"}):
            response = client.post("/users/validate-access-code", json={"email": sample_user.email, "code": "123456"})
            assert response.status_code == 200
            assert response.json()["access_token"] == "fake-token"

    def test_validate_access_code_invalid(
        self,
        client: TestClient,
        sample_user: User,
    ):
        with patch("utils.oauth2_integration.oauth2_authenticator.authenticate_user_with_code", return_value=None):
            response = client.post("/users/validate-access-code", json={"email": sample_user.email, "code": "wrong-code"})
            assert response.status_code == 401
            assert response.json()["detail"] == "Invalid or expired access code"


class TestDeleteUser:
    def test_delete_user_returns_200(
        self,
        client: TestClient,
        authenticated_user: User,
        sample_admin_user: User,
    ):
        response = client.delete(f"/users/{sample_admin_user.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True

    def test_delete_user_not_found_returns_404(
        self, client: TestClient, authenticated_user: User
    ):
        response = client.delete("/users/nonexistent-id")
        assert response.status_code == 404
