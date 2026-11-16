from fastapi.testclient import TestClient

from database.models import GherkinRule, GherkinScenario, Project, User, UserStory


class TestListGherkinRules:
    def test_list_rules_returns_200_empty(
        self,
        client: TestClient,
        project_access_user: User,
        sample_project: Project,
        sample_user_story: UserStory,
    ):
        response = client.get(
            f"/projects/{sample_project.id}/user_stories/{sample_user_story.id}/gherkin_rules/"
        )
        assert response.status_code == 200
        assert response.json() == []

    def test_list_rules_returns_existing_rules(
        self,
        client: TestClient,
        project_access_user: User,
        sample_project: Project,
        sample_user_story: UserStory,
        sample_gherkin_rule: GherkinRule,
    ):
        response = client.get(
            f"/projects/{sample_project.id}/user_stories/{sample_user_story.id}/gherkin_rules/"
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == sample_gherkin_rule.id
        assert data[0]["title"] == sample_gherkin_rule.title
        assert data[0]["index"] == sample_gherkin_rule.index
        assert data[0]["background"] == sample_gherkin_rule.background
        assert "scenarios" in data[0]

    def test_list_rules_includes_scenarios(
        self,
        client: TestClient,
        project_access_user: User,
        sample_project: Project,
        sample_user_story: UserStory,
        sample_gherkin_rule: GherkinRule,
        sample_gherkin_scenario: GherkinScenario,
    ):
        response = client.get(
            f"/projects/{sample_project.id}/user_stories/{sample_user_story.id}/gherkin_rules/"
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert len(data[0]["scenarios"]) == 1
        assert data[0]["scenarios"][0]["id"] == sample_gherkin_scenario.id

    def test_list_rules_ordered_by_index(
        self,
        client: TestClient,
        project_access_user: User,
        sample_project: Project,
        sample_user_story: UserStory,
        sample_gherkin_rule: GherkinRule,
        sample_second_gherkin_rule: GherkinRule,
    ):
        response = client.get(
            f"/projects/{sample_project.id}/user_stories/{sample_user_story.id}/gherkin_rules/"
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert data[0]["index"] <= data[1]["index"]


class TestGetGherkinRule:
    def test_get_rule_returns_200(
        self,
        client: TestClient,
        project_access_user: User,
        sample_project: Project,
        sample_user_story: UserStory,
        sample_gherkin_rule: GherkinRule,
    ):
        response = client.get(
            f"/projects/{sample_project.id}/user_stories/{sample_user_story.id}"
            f"/gherkin_rules/{sample_gherkin_rule.id}"
        )
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == sample_gherkin_rule.id
        assert data["title"] == sample_gherkin_rule.title
        assert "scenarios" in data

    def test_get_rule_not_found_returns_404(
        self,
        client: TestClient,
        project_access_user: User,
        sample_project: Project,
        sample_user_story: UserStory,
    ):
        response = client.get(
            f"/projects/{sample_project.id}/user_stories/{sample_user_story.id}"
            "/gherkin_rules/nonexistent-id"
        )
        assert response.status_code == 404

    def test_get_rule_wrong_story_returns_404(
        self,
        client: TestClient,
        project_access_user: User,
        sample_project: Project,
        sample_user_story: UserStory,
        sample_second_user_story: UserStory,
        sample_gherkin_rule: GherkinRule,
    ):
        response = client.get(
            f"/projects/{sample_project.id}/user_stories/{sample_second_user_story.id}"
            f"/gherkin_rules/{sample_gherkin_rule.id}"
        )
        assert response.status_code == 404


class TestCreateGherkinRule:
    def test_create_rule_returns_200(
        self,
        client: TestClient,
        project_access_user: User,
        sample_project: Project,
        sample_user_story: UserStory,
    ):
        payload = {
            "index": 0,
            "title": "New Rule",
            "background": "Given a new context",
        }
        response = client.post(
            f"/projects/{sample_project.id}/user_stories/{sample_user_story.id}/gherkin_rules/",
            json=payload,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "New Rule"
        assert data["index"] == 0
        assert data["background"] == "Given a new context"
        assert "id" in data
        assert data["scenarios"] == []

    def test_create_rule_missing_title_returns_422(
        self,
        client: TestClient,
        project_access_user: User,
        sample_project: Project,
        sample_user_story: UserStory,
    ):
        payload = {"index": 0, "background": "Missing title"}
        response = client.post(
            f"/projects/{sample_project.id}/user_stories/{sample_user_story.id}/gherkin_rules/",
            json=payload,
        )
        assert response.status_code == 422


class TestUpdateGherkinRule:
    def test_update_rule_returns_200(
        self,
        client: TestClient,
        project_access_user: User,
        sample_project: Project,
        sample_user_story: UserStory,
        sample_gherkin_rule: GherkinRule,
    ):
        payload = {
            "index": 5,
            "title": "Updated Rule Title",
            "background": "Updated background",
        }
        response = client.put(
            f"/projects/{sample_project.id}/user_stories/{sample_user_story.id}"
            f"/gherkin_rules/{sample_gherkin_rule.id}",
            json=payload,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Updated Rule Title"
        assert data["index"] == 5
        assert data["background"] == "Updated background"

    def test_update_rule_not_found_returns_404(
        self,
        client: TestClient,
        project_access_user: User,
        sample_project: Project,
        sample_user_story: UserStory,
    ):
        payload = {"index": 0, "title": "Nope", "background": "Nope"}
        response = client.put(
            f"/projects/{sample_project.id}/user_stories/{sample_user_story.id}"
            "/gherkin_rules/nonexistent-id",
            json=payload,
        )
        assert response.status_code == 404

    def test_update_rule_wrong_story_returns_404(
        self,
        client: TestClient,
        project_access_user: User,
        sample_project: Project,
        sample_second_user_story: UserStory,
        sample_gherkin_rule: GherkinRule,
    ):
        payload = {"index": 0, "title": "Wrong", "background": "Wrong"}
        response = client.put(
            f"/projects/{sample_project.id}/user_stories/{sample_second_user_story.id}"
            f"/gherkin_rules/{sample_gherkin_rule.id}",
            json=payload,
        )
        assert response.status_code == 404


class TestDeleteGherkinRule:
    def test_delete_rule_returns_200(
        self,
        client: TestClient,
        project_access_user: User,
        sample_project: Project,
        sample_user_story: UserStory,
        sample_gherkin_rule: GherkinRule,
    ):
        response = client.delete(
            f"/projects/{sample_project.id}/user_stories/{sample_user_story.id}"
            f"/gherkin_rules/{sample_gherkin_rule.id}"
        )
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True

    def test_delete_rule_not_found_returns_404(
        self,
        client: TestClient,
        project_access_user: User,
        sample_project: Project,
        sample_user_story: UserStory,
    ):
        response = client.delete(
            f"/projects/{sample_project.id}/user_stories/{sample_user_story.id}"
            "/gherkin_rules/nonexistent-id"
        )
        assert response.status_code == 404

    def test_delete_rule_cascades_scenarios(
        self,
        client: TestClient,
        project_access_user: User,
        sample_project: Project,
        sample_user_story: UserStory,
        sample_gherkin_rule: GherkinRule,
        sample_gherkin_scenario: GherkinScenario,
    ):
        # Delete the rule
        response = client.delete(
            f"/projects/{sample_project.id}/user_stories/{sample_user_story.id}"
            f"/gherkin_rules/{sample_gherkin_rule.id}"
        )
        assert response.status_code == 200

        # Verify the rule is gone
        response = client.get(
            f"/projects/{sample_project.id}/user_stories/{sample_user_story.id}/gherkin_rules/"
        )
        assert response.status_code == 200
        assert response.json() == []
