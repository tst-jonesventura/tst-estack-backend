from fastapi.testclient import TestClient

from database.models import GherkinRule, GherkinScenario, Project, User, UserStory


class TestListGherkinScenarios:
    def test_list_scenarios_returns_200_empty(
        self,
        client: TestClient,
        project_access_user: User,
        sample_project: Project,
        sample_user_story: UserStory,
        sample_gherkin_rule: GherkinRule,
    ):
        response = client.get(
            f"/projects/{sample_project.id}/user_stories/{sample_user_story.id}"
            f"/gherkin_rules/{sample_gherkin_rule.id}/scenarios/"
        )
        assert response.status_code == 200
        assert response.json() == []

    def test_list_scenarios_returns_existing(
        self,
        client: TestClient,
        project_access_user: User,
        sample_project: Project,
        sample_user_story: UserStory,
        sample_gherkin_rule: GherkinRule,
        sample_gherkin_scenario: GherkinScenario,
    ):
        response = client.get(
            f"/projects/{sample_project.id}/user_stories/{sample_user_story.id}"
            f"/gherkin_rules/{sample_gherkin_rule.id}/scenarios/"
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == sample_gherkin_scenario.id
        assert data[0]["title"] == sample_gherkin_scenario.title
        assert data[0]["text"] == sample_gherkin_scenario.text
        assert data[0]["is_done"] == sample_gherkin_scenario.is_done
        assert data[0]["gherkin_rule_id"] == sample_gherkin_rule.id

    def test_list_scenarios_ordered_by_index(
        self,
        client: TestClient,
        project_access_user: User,
        sample_project: Project,
        sample_user_story: UserStory,
        sample_gherkin_rule: GherkinRule,
        sample_gherkin_scenario: GherkinScenario,
        sample_second_gherkin_scenario: GherkinScenario,
    ):
        response = client.get(
            f"/projects/{sample_project.id}/user_stories/{sample_user_story.id}"
            f"/gherkin_rules/{sample_gherkin_rule.id}/scenarios/"
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert data[0]["index"] <= data[1]["index"]


class TestGetGherkinScenario:
    def test_get_scenario_returns_200(
        self,
        client: TestClient,
        project_access_user: User,
        sample_project: Project,
        sample_user_story: UserStory,
        sample_gherkin_rule: GherkinRule,
        sample_gherkin_scenario: GherkinScenario,
    ):
        response = client.get(
            f"/projects/{sample_project.id}/user_stories/{sample_user_story.id}"
            f"/gherkin_rules/{sample_gherkin_rule.id}/scenarios/{sample_gherkin_scenario.id}"
        )
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == sample_gherkin_scenario.id
        assert data["title"] == sample_gherkin_scenario.title

    def test_get_scenario_not_found_returns_404(
        self,
        client: TestClient,
        project_access_user: User,
        sample_project: Project,
        sample_user_story: UserStory,
        sample_gherkin_rule: GherkinRule,
    ):
        response = client.get(
            f"/projects/{sample_project.id}/user_stories/{sample_user_story.id}"
            f"/gherkin_rules/{sample_gherkin_rule.id}/scenarios/nonexistent-id"
        )
        assert response.status_code == 404

    def test_get_scenario_wrong_rule_returns_404(
        self,
        client: TestClient,
        project_access_user: User,
        sample_project: Project,
        sample_user_story: UserStory,
        sample_gherkin_rule: GherkinRule,
        sample_second_gherkin_rule: GherkinRule,
        sample_gherkin_scenario: GherkinScenario,
    ):
        response = client.get(
            f"/projects/{sample_project.id}/user_stories/{sample_user_story.id}"
            f"/gherkin_rules/{sample_second_gherkin_rule.id}"
            f"/scenarios/{sample_gherkin_scenario.id}"
        )
        assert response.status_code == 404


class TestCreateGherkinScenario:
    def test_create_scenario_returns_200(
        self,
        client: TestClient,
        project_access_user: User,
        sample_project: Project,
        sample_user_story: UserStory,
        sample_gherkin_rule: GherkinRule,
    ):
        payload = {
            "index": 0,
            "title": "New Scenario",
            "text": "When something Then something else",
            "is_done": False,
        }
        response = client.post(
            f"/projects/{sample_project.id}/user_stories/{sample_user_story.id}"
            f"/gherkin_rules/{sample_gherkin_rule.id}/scenarios/",
            json=payload,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "New Scenario"
        assert data["text"] == "When something Then something else"
        assert data["is_done"] is False
        assert data["gherkin_rule_id"] == sample_gherkin_rule.id
        assert "id" in data

    def test_create_scenario_missing_title_returns_422(
        self,
        client: TestClient,
        project_access_user: User,
        sample_project: Project,
        sample_user_story: UserStory,
        sample_gherkin_rule: GherkinRule,
    ):
        payload = {"index": 0, "text": "Missing title", "is_done": False}
        response = client.post(
            f"/projects/{sample_project.id}/user_stories/{sample_user_story.id}"
            f"/gherkin_rules/{sample_gherkin_rule.id}/scenarios/",
            json=payload,
        )
        assert response.status_code == 422

    def test_create_scenario_is_done_defaults_false(
        self,
        client: TestClient,
        project_access_user: User,
        sample_project: Project,
        sample_user_story: UserStory,
        sample_gherkin_rule: GherkinRule,
    ):
        payload = {
            "index": 0,
            "title": "Default Done",
            "text": "When testing defaults Then is_done is false",
        }
        response = client.post(
            f"/projects/{sample_project.id}/user_stories/{sample_user_story.id}"
            f"/gherkin_rules/{sample_gherkin_rule.id}/scenarios/",
            json=payload,
        )
        assert response.status_code == 200
        assert response.json()["is_done"] is False


class TestUpdateGherkinScenario:
    def test_update_scenario_returns_200(
        self,
        client: TestClient,
        project_access_user: User,
        sample_project: Project,
        sample_user_story: UserStory,
        sample_gherkin_rule: GherkinRule,
        sample_gherkin_scenario: GherkinScenario,
    ):
        payload = {
            "index": 10,
            "title": "Updated Scenario",
            "text": "Updated text",
            "is_done": True,
        }
        response = client.put(
            f"/projects/{sample_project.id}/user_stories/{sample_user_story.id}"
            f"/gherkin_rules/{sample_gherkin_rule.id}"
            f"/scenarios/{sample_gherkin_scenario.id}",
            json=payload,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Updated Scenario"
        assert data["text"] == "Updated text"
        assert data["index"] == 10
        assert data["is_done"] is True

    def test_update_scenario_not_found_returns_404(
        self,
        client: TestClient,
        project_access_user: User,
        sample_project: Project,
        sample_user_story: UserStory,
        sample_gherkin_rule: GherkinRule,
    ):
        payload = {
            "index": 0,
            "title": "Nope",
            "text": "Nope",
            "is_done": False,
        }
        response = client.put(
            f"/projects/{sample_project.id}/user_stories/{sample_user_story.id}"
            f"/gherkin_rules/{sample_gherkin_rule.id}/scenarios/nonexistent-id",
            json=payload,
        )
        assert response.status_code == 404

    def test_update_scenario_wrong_rule_returns_404(
        self,
        client: TestClient,
        project_access_user: User,
        sample_project: Project,
        sample_user_story: UserStory,
        sample_second_gherkin_rule: GherkinRule,
        sample_gherkin_scenario: GherkinScenario,
    ):
        payload = {
            "index": 0,
            "title": "Wrong",
            "text": "Wrong",
            "is_done": False,
        }
        response = client.put(
            f"/projects/{sample_project.id}/user_stories/{sample_user_story.id}"
            f"/gherkin_rules/{sample_second_gherkin_rule.id}"
            f"/scenarios/{sample_gherkin_scenario.id}",
            json=payload,
        )
        assert response.status_code == 404


class TestDeleteGherkinScenario:
    def test_delete_scenario_returns_200(
        self,
        client: TestClient,
        project_access_user: User,
        sample_project: Project,
        sample_user_story: UserStory,
        sample_gherkin_rule: GherkinRule,
        sample_gherkin_scenario: GherkinScenario,
    ):
        response = client.delete(
            f"/projects/{sample_project.id}/user_stories/{sample_user_story.id}"
            f"/gherkin_rules/{sample_gherkin_rule.id}"
            f"/scenarios/{sample_gherkin_scenario.id}"
        )
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True

    def test_delete_scenario_not_found_returns_404(
        self,
        client: TestClient,
        project_access_user: User,
        sample_project: Project,
        sample_user_story: UserStory,
        sample_gherkin_rule: GherkinRule,
    ):
        response = client.delete(
            f"/projects/{sample_project.id}/user_stories/{sample_user_story.id}"
            f"/gherkin_rules/{sample_gherkin_rule.id}/scenarios/nonexistent-id"
        )
        assert response.status_code == 404

    def test_delete_scenario_then_list_empty(
        self,
        client: TestClient,
        project_access_user: User,
        sample_project: Project,
        sample_user_story: UserStory,
        sample_gherkin_rule: GherkinRule,
        sample_gherkin_scenario: GherkinScenario,
    ):
        # Delete the scenario
        response = client.delete(
            f"/projects/{sample_project.id}/user_stories/{sample_user_story.id}"
            f"/gherkin_rules/{sample_gherkin_rule.id}"
            f"/scenarios/{sample_gherkin_scenario.id}"
        )
        assert response.status_code == 200

        # Verify it's gone
        response = client.get(
            f"/projects/{sample_project.id}/user_stories/{sample_user_story.id}"
            f"/gherkin_rules/{sample_gherkin_rule.id}/scenarios/"
        )
        assert response.status_code == 200
        assert response.json() == []
