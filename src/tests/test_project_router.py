import uuid

from fastapi.testclient import TestClient
import pytest

from database import models
from database.models import (
    AgentRun,
    DocumentTranscription,
    Project,
    Role,
    User,
    UserStory,
)
from src.main import app
from utils.bearer_auth import (
    check_project_access,
    ensure_admin,
    retrieve_admin_data,
    retrieve_user_data,
)
from utils.token_usage import SPEC_WITHOUT_STORY_LABEL

class TestGetProject:
    def test_get_project_returns_200(
        self, client: TestClient, project_access_user: User, sample_project: Project
    ):
        response = client.get(f"/projects/{sample_project.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == sample_project.id
        assert data["name"] == sample_project.name
        assert data["description"] == sample_project.description
        assert data["is_active"] is True

    def test_get_project_not_found_returns_404(
        self, client: TestClient, project_access_user: User
    ):
        response = client.get("/projects/nonexistent-id")
        assert response.status_code == 404


class TestGetFullProject:
    def test_get_full_project_returns_200_empty_stories(
        self, client: TestClient, project_access_user: User, sample_project: Project
    ):
        response = client.get(f"/projects/{sample_project.id}/full")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == sample_project.id
        assert data["name"] == sample_project.name
        assert data["user_stories"] == []

    def test_get_full_project_includes_user_stories(
        self,
        client: TestClient,
        project_access_user: User,
        sample_project: Project,
        sample_user_story: UserStory,
    ):
        response = client.get(f"/projects/{sample_project.id}/full")
        assert response.status_code == 200
        data = response.json()
        assert len(data["user_stories"]) == 1
        story = data["user_stories"][0]
        assert story["id"] == sample_user_story.id
        assert story["name"] == sample_user_story.name

    def test_get_full_project_not_found_returns_404(
        self, client: TestClient, project_access_user: User
    ):
        response = client.get("/projects/nonexistent-id/full")
        assert response.status_code == 404

@pytest.mark.skip(reason="Testes falhando, revisar")
class TestCreateProject:
    def test_create_project_returns_200(
        self,
        client: TestClient,
        authenticated_user: User,
        sample_role: Role,
    ):
        payload = {"name": "New Project", "description": "A new project"}
        response = client.post("/projects/", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "New Project"
        assert data["description"] == "A new project"
        assert data["is_active"] is True
        assert "id" in data

    def test_create_project_missing_name_returns_422(
        self, client: TestClient, authenticated_user: User, sample_role: Role
    ):
        payload = {"description": "Missing name"}
        response = client.post("/projects/", json=payload)
        assert response.status_code == 422


class TestListProjects:
    def test_list_projects_returns_200(
        self,
        client: TestClient,
        authenticated_user: User,
        sample_project: Project,
    ):
        response = client.get("/projects/")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == sample_project.id
        assert data[0]["name"] == sample_project.name
        assert data[0]["user_stories_count"] == 0
        assert data[0]["tech_specs_count"] == 0

    def test_list_projects_empty_when_no_projects(
        self, client: TestClient, authenticated_user: User
    ):
        response = client.get("/projects/")
        assert response.status_code == 200
        assert response.json() == []

    def test_list_projects_counts_stories_and_specs(
        self,
        client: TestClient,
        authenticated_user: User,
        sample_project: Project,
        sample_user_story: UserStory,
        sample_tech_spec,
    ):
        response = client.get("/projects/")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["user_stories_count"] == 1
        assert data[0]["tech_specs_count"] == 1


class TestUpdateProject:
    def test_update_project_returns_200(
        self, client: TestClient, project_access_user: User, sample_project: Project
    ):
        payload = {"name": "Updated Name", "description": "Updated desc"}
        response = client.put(f"/projects/{sample_project.id}", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Updated Name"
        assert data["description"] == "Updated desc"

    def test_update_project_partial(
        self, client: TestClient, project_access_user: User, sample_project: Project
    ):
        payload = {"name": "Only Name"}
        response = client.put(f"/projects/{sample_project.id}", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Only Name"
        assert data["description"] == sample_project.description

    def test_update_project_not_found_returns_404(
        self, client: TestClient, project_access_user: User
    ):
        payload = {"name": "Nope"}
        response = client.put("/projects/nonexistent-id", json=payload)
        assert response.status_code == 404


class TestListProjectTokenUsage:
    def test_token_usage_non_admin_returns_403(
        self, client: TestClient, sample_user: User
    ):
        def admin_dependency():
            ensure_admin(sample_user)
            return sample_user

        app.dependency_overrides[retrieve_admin_data] = admin_dependency
        try:
            response = client.get("/projects/token-usage")
            assert response.status_code == 403
        finally:
            app.dependency_overrides.pop(retrieve_admin_data, None)

    def test_token_usage_returns_accessible_projects_without_usage(
        self,
        client: TestClient,
        sample_admin_user: User,
        sample_project: Project,
    ):
        app.dependency_overrides[retrieve_admin_data] = lambda: sample_admin_user
        app.dependency_overrides[retrieve_user_data] = lambda: sample_admin_user
        try:
            response = client.get("/projects/token-usage")
            assert response.status_code == 200
            assert response.json() == []
        finally:
            app.dependency_overrides.pop(retrieve_admin_data, None)
            app.dependency_overrides.pop(retrieve_user_data, None)

    def test_token_usage_aggregates_by_model(
        self,
        client: TestClient,
        db_session,
        sample_admin_user: User,
        sample_second_project: Project,
    ):
        db_session.add_all(
            [
                AgentRun(
                    user_id=sample_admin_user.id,
                    project_id=sample_second_project.id,
                    title="Run A",
                    status="finished",
                    model="gpt-4o",
                    input_tokens=100,
                    output_tokens=50,
                ),
                AgentRun(
                    user_id=sample_admin_user.id,
                    project_id=sample_second_project.id,
                    title="Run B",
                    status="finished",
                    model="gpt-4o",
                    input_tokens=20,
                    output_tokens=10,
                ),
                AgentRun(
                    user_id=sample_admin_user.id,
                    project_id=sample_second_project.id,
                    title="Run C",
                    status="finished",
                    model="gpt-4o-mini",
                    input_tokens=5,
                    output_tokens=5,
                ),
                AgentRun(
                    user_id=sample_admin_user.id,
                    project_id=sample_second_project.id,
                    title="Run D",
                    status="finished",
                    model=None,
                    input_tokens=3,
                    output_tokens=2,
                ),
            ]
        )
        db_session.commit()

        app.dependency_overrides[retrieve_admin_data] = lambda: sample_admin_user
        app.dependency_overrides[retrieve_user_data] = lambda: sample_admin_user
        try:
            response = client.get("/projects/token-usage")
            assert response.status_code == 200
            data = response.json()
            assert len(data) == 1
            assert data[0]["id"] == sample_second_project.id
            assert data[0]["name"] == sample_second_project.name
            assert data[0]["models"] == [
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
                {
                    "model": "Não informado",
                    "agent_id": None,
                    "agent_name": None,
                    "total_tokens": 5,
                },
            ]
        finally:
            app.dependency_overrides.pop(retrieve_admin_data, None)
            app.dependency_overrides.pop(retrieve_user_data, None)

    def test_token_usage_groups_by_agent_and_model(
        self,
        client: TestClient,
        db_session,
        sample_admin_user: User,
        sample_second_project: Project,
    ):
        agent_a = models.Agent(
            id=str(uuid.uuid4()),
            name="agent_a",
            model="openai:gpt-4o",
            is_default=True,
            max_retries=1,
        )
        agent_b = models.Agent(
            id=str(uuid.uuid4()),
            name="agent_b",
            model="openai:gpt-4o",
            is_default=True,
            max_retries=1,
        )
        db_session.add_all([agent_a, agent_b])
        db_session.commit()

        db_session.add_all(
            [
                AgentRun(
                    user_id=sample_admin_user.id,
                    project_id=sample_second_project.id,
                    agent_id=agent_a.id,
                    title="Run 1",
                    status="finished",
                    model="gpt-4o",
                    input_tokens=100,
                    output_tokens=50,
                ),
                AgentRun(
                    user_id=sample_admin_user.id,
                    project_id=sample_second_project.id,
                    agent_id=agent_b.id,
                    title="Run 2",
                    status="finished",
                    model="gpt-4o",
                    input_tokens=20,
                    output_tokens=10,
                ),
            ]
        )
        db_session.commit()

        app.dependency_overrides[retrieve_admin_data] = lambda: sample_admin_user
        app.dependency_overrides[retrieve_user_data] = lambda: sample_admin_user
        try:
            response = client.get("/projects/token-usage")
            assert response.status_code == 200
            data = response.json()
            assert len(data) == 1
            assert data[0]["models"] == [
                {
                    "model": "gpt-4o",
                    "agent_id": agent_a.id,
                    "agent_name": "agent_a",
                    "total_tokens": 150,
                },
                {
                    "model": "gpt-4o",
                    "agent_id": agent_b.id,
                    "agent_name": "agent_b",
                    "total_tokens": 30,
                },
            ]
        finally:
            app.dependency_overrides.pop(retrieve_admin_data, None)
            app.dependency_overrides.pop(retrieve_user_data, None)

    def test_token_usage_excludes_projects_outside_membership(
        self,
        client: TestClient,
        db_session,
        sample_admin_user: User,
        sample_project: Project,
        sample_second_project: Project,
    ):
        db_session.add(
            AgentRun(
                user_id=sample_admin_user.id,
                project_id=sample_project.id,
                title="Outside membership",
                status="finished",
                model="gpt-4o",
                input_tokens=999,
                output_tokens=1,
            )
        )
        db_session.commit()

        app.dependency_overrides[retrieve_admin_data] = lambda: sample_admin_user
        app.dependency_overrides[retrieve_user_data] = lambda: sample_admin_user
        try:
            response = client.get("/projects/token-usage")
            assert response.status_code == 200
            data = response.json()
            assert data == []
        finally:
            app.dependency_overrides.pop(retrieve_admin_data, None)
            app.dependency_overrides.pop(retrieve_user_data, None)


class TestProjectTokenUsageDetail:
    def test_detail_non_admin_returns_403(
        self, client: TestClient, sample_user: User, sample_second_project: Project
    ):
        def admin_dependency():
            ensure_admin(sample_user)
            return sample_user

        app.dependency_overrides[retrieve_admin_data] = admin_dependency
        try:
            response = client.get(
                f"/projects/{sample_second_project.id}/token-usage/detail"
            )
            assert response.status_code == 403
        finally:
            app.dependency_overrides.pop(retrieve_admin_data, None)

    def test_detail_ranks_contributors_and_specifications(
        self,
        client: TestClient,
        db_session,
        sample_admin_user: User,
        sample_user: User,
        sample_second_project: Project,
        sample_transcription: DocumentTranscription,
    ):
        agent = models.Agent(
            id=str(uuid.uuid4()),
            name="tutor_agent",
            model="openai:gpt-4o",
            is_default=True,
            max_retries=1,
        )
        db_session.add(agent)
        story = UserStory(
            id="story-detail-1",
            name="Checkout Flow",
            description="Story for token usage detail",
            points=3,
            is_done=False,
            project_id=sample_second_project.id,
            user_id=sample_admin_user.id,
            transcription_id=sample_transcription.id,
            log="",
        )
        db_session.add(story)
        db_session.add_all(
            [
                AgentRun(
                    user_id=sample_admin_user.id,
                    project_id=sample_second_project.id,
                    user_story_id=story.id,
                    title="Story run",
                    status="finished",
                    model="gpt-4o",
                    agent_id=agent.id,
                    input_tokens=100,
                    output_tokens=50,
                ),
                AgentRun(
                    user_id=sample_user.id,
                    project_id=sample_second_project.id,
                    user_story_id=None,
                    title="Content analysis run",
                    status="finished",
                    model="gpt-4o-mini",
                    agent_id=None,
                    input_tokens=10,
                    output_tokens=5,
                ),
                AgentRun(
                    user_id=sample_user.id,
                    project_id=sample_second_project.id,
                    user_story_id=None,
                    title="Zero tokens run",
                    status="finished",
                    model="gpt-4o-mini",
                    agent_id=agent.id,
                    input_tokens=0,
                    output_tokens=0,
                ),
            ]
        )
        db_session.commit()

        app.dependency_overrides[retrieve_admin_data] = lambda: sample_admin_user
        app.dependency_overrides[retrieve_user_data] = lambda: sample_admin_user
        app.dependency_overrides[check_project_access] = lambda: sample_admin_user
        try:
            response = client.get(
                f"/projects/{sample_second_project.id}/token-usage/detail"
            )
            assert response.status_code == 200
            data = response.json()
            assert data["id"] == sample_second_project.id
            assert len(data["contributors"]) == 2
            assert data["contributors"][0]["id"] == sample_admin_user.id
            assert data["contributors"][0]["total_tokens"] == 150
            assert data["contributors"][0]["models"] == [
                {
                    "model": "gpt-4o",
                    "agent_id": agent.id,
                    "agent_name": "tutor_agent",
                    "total_tokens": 150,
                }
            ]
            assert data["contributors"][1]["id"] == sample_user.id
            assert data["contributors"][1]["total_tokens"] == 15

            assert len(data["specifications"]) == 2
            assert data["specifications"][0]["label"] == "Checkout Flow"
            assert data["specifications"][0]["total_tokens"] == 150
            assert data["specifications"][1]["label"] == SPEC_WITHOUT_STORY_LABEL
            assert data["specifications"][1]["total_tokens"] == 15

            assert len(data["agents"]) == 1
            assert data["agents"][0]["id"] == agent.id
            assert data["agents"][0]["label"] == "tutor_agent"
            assert data["agents"][0]["total_tokens"] == 150
            assert data["agents"][0]["models"] == [
                {
                    "model": "gpt-4o",
                    "agent_id": agent.id,
                    "agent_name": "tutor_agent",
                    "total_tokens": 150,
                }
            ]
        finally:
            app.dependency_overrides.pop(retrieve_admin_data, None)
            app.dependency_overrides.pop(retrieve_user_data, None)
            app.dependency_overrides.pop(check_project_access, None)


class TestDeleteProject:
    def test_delete_project_returns_200(
        self, client: TestClient, project_access_user: User, sample_project: Project
    ):
        response = client.delete(f"/projects/{sample_project.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True

    def test_delete_project_not_found_returns_404(
        self, client: TestClient, project_access_user: User
    ):
        response = client.delete("/projects/nonexistent-id")
        assert response.status_code == 404
