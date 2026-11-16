from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from database.models import (
    JiraIntegrationCardsMapping,
    JiraIntegrationColumnsMapping,
    JiraIntegrationConfig,
    Project,
    ProjectKanbanColumn,
    UserStory,
)
from integrations.jira_provider import JiraIntegrationProvider
from services.kanban_service import KanbanService


@pytest.fixture
def mock_session():
    return MagicMock(spec=Session)


@pytest.fixture
def mock_project():
    return Project(id="project_id", name="Test Project")


@pytest.fixture
def mock_story():
    return UserStory(
        id="story_id",
        name="Test Story",
        description="Description",
        project_id="project_id",
    )


@pytest.fixture
def mock_config():
    return JiraIntegrationConfig(
        id="config_id",
        project_id="project_id",
        jira_base_url="https://test.atlassian.net",
        jira_email="test@example.com",
        jira_api_token="token",
        jira_project_key="TEST",
    )


@pytest.fixture
def mock_kanban_column():
    return ProjectKanbanColumn(
        id="col_id",
        name="In Progress",
        position=1,
        project_id="project_id",
    )


@pytest.fixture
def mock_httpx_client():
    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client
        yield mock_client


@pytest.fixture
def mock_card_mapping(mock_story):
    return JiraIntegrationCardsMapping(
        user_story_id=mock_story.id, jira_issue_key="TEST-123"
    )


@pytest.fixture
def mock_column_mapping(mock_config, mock_kanban_column):
    return JiraIntegrationColumnsMapping(
        id="cm_1",
        jira_integration_config_id=mock_config.id,
        project_kanban_column_id=mock_kanban_column.id,
        jira_status_id="10002",
        jira_status_name="In Progress",
    )


def configure_session_models(mock_session, model_mapping=None):
    mapping = model_mapping or {}
    mock_session.query.side_effect = lambda model: MagicMock(
        filter=MagicMock(
            return_value=MagicMock(
                first=MagicMock(return_value=mapping.get(model))
            )
        )
    )


@pytest.mark.anyio
async def test_create_issue_success(
    mock_session, mock_project, mock_story, mock_config, mock_httpx_client
):
    configure_session_models(mock_session, {JiraIntegrationConfig: mock_config})
    mock_httpx_client.post.return_value = MagicMock(
        json=MagicMock(return_value={"key": "TEST-1"}),
        raise_for_status=MagicMock(),
    )

    provider = JiraIntegrationProvider(mock_session)
    issue_key = await provider.create_issue(mock_project, mock_story)

    assert issue_key == "TEST-1"
    mock_httpx_client.post.assert_called_once()
    assert (
        mock_httpx_client.post.call_args[1]["json"]["fields"]["summary"]
        == "Test Story"
    )


@pytest.mark.anyio
async def test_create_issue_no_config(mock_session, mock_project, mock_story):
    configure_session_models(mock_session)
    provider = JiraIntegrationProvider(mock_session)

    with pytest.raises(
        ValueError, match="Jira integration not configured for this project"
    ):
        await provider.create_issue(mock_project, mock_story)


@pytest.mark.anyio
async def test_update_issue_success(
    mock_session,
    mock_project,
    mock_story,
    mock_config,
    mock_card_mapping,
    mock_httpx_client,
):
    configure_session_models(
        mock_session,
        {
            JiraIntegrationCardsMapping: mock_card_mapping,
            JiraIntegrationConfig: mock_config,
        },
    )
    mock_httpx_client.put.return_value = MagicMock(raise_for_status=MagicMock())

    provider = JiraIntegrationProvider(mock_session)
    await provider.update_issue(mock_project, mock_story)

    mock_httpx_client.put.assert_called_once()
    assert (
        mock_httpx_client.put.call_args[1]["json"]["fields"]["summary"]
        == "Test Story"
    )


@pytest.mark.anyio
async def test_update_issue_no_card_mapping(mock_session, mock_project, mock_story):
    configure_session_models(mock_session)
    provider = JiraIntegrationProvider(mock_session)

    with pytest.raises(ValueError, match="Jira issue mapping not found for this user story"):
        await provider.update_issue(mock_project, mock_story)


@pytest.mark.anyio
async def test_delete_issue_success(
    mock_session,
    mock_project,
    mock_story,
    mock_config,
    mock_card_mapping,
    mock_httpx_client,
):
    configure_session_models(
        mock_session,
        {
            JiraIntegrationCardsMapping: mock_card_mapping,
            JiraIntegrationConfig: mock_config,
        },
    )
    mock_httpx_client.delete.return_value = MagicMock(raise_for_status=MagicMock())

    provider = JiraIntegrationProvider(mock_session)
    await provider.delete_issue(mock_project, mock_story)

    expected_url = f"{mock_config.jira_base_url}/rest/api/3/issue/{mock_card_mapping.jira_issue_key}"
    mock_httpx_client.delete.assert_called_once_with(
        expected_url,
        auth=(mock_config.jira_email, mock_config.jira_api_token),
    )
    mock_session.delete.assert_called_once_with(mock_card_mapping)
    mock_session.commit.assert_called_once()


@pytest.mark.anyio
async def test_delete_issue_no_card_mapping(
    mock_session, mock_project, mock_story, mock_httpx_client
):
    configure_session_models(mock_session)
    provider = JiraIntegrationProvider(mock_session)

    await provider.delete_issue(mock_project, mock_story)

    mock_httpx_client.delete.assert_not_called()
    mock_session.delete.assert_not_called()
    mock_session.commit.assert_not_called()


@pytest.mark.parametrize(
    ("jira_status_id", "jira_status_name", "transitions", "expected_transition_id"),
    [
        (
            "10002",
            "In Progress",
            [
                {"id": "10002", "name": "Start", "to": {"id": "9999", "name": "Other"}},
            ],
            "10002",
        ),
        (
            "",
            "start working",
            [
                {"id": "50", "name": "start working", "to": {"id": "9999", "name": "Other"}},
            ],
            "50",
        ),
        (
            "",
            "",
            [
                {"id": "50", "name": "start working", "to": {"id": "9999", "name": "Other"}},
            ],
            None,
        ),
    ],
)
def test_find_matching_transition_id_branches(
    jira_status_id, jira_status_name, transitions, expected_transition_id
):
    col_mapping = JiraIntegrationColumnsMapping(
        jira_status_id=jira_status_id,
        jira_status_name=jira_status_name,
    )
    res = JiraIntegrationProvider._find_matching_transition_id(transitions, col_mapping)
    assert res == expected_transition_id


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("jira_status_id", "jira_status_name", "transitions", "expected_transition_id"),
    [
        (
            "10002",
            "In Progress",
            [
                {"id": "11", "name": "To Do", "to": {"id": "10001", "name": "To Do"}},
                {
                    "id": "21",
                    "name": "In Progress",
                    "to": {"id": "10002", "name": "In Progress"},
                },
            ],
            "21",
        ),
        (
            "other_id",
            "In Progress",
            [
                {
                    "id": "31",
                    "name": "Start Working",
                    "to": {"id": "9999", "name": "in progress"},
                },
            ],
            "31",
        ),
    ],
)
async def test_update_issue_column_success(
    mock_session,
    mock_project,
    mock_story,
    mock_config,
    mock_kanban_column,
    mock_card_mapping,
    mock_httpx_client,
    jira_status_id,
    jira_status_name,
    transitions,
    expected_transition_id,
):
    col_mapping = JiraIntegrationColumnsMapping(
        id="cm_1",
        jira_integration_config_id=mock_config.id,
        project_kanban_column_id=mock_kanban_column.id,
        jira_status_id=jira_status_id,
        jira_status_name=jira_status_name,
    )
    configure_session_models(
        mock_session,
        {
            JiraIntegrationCardsMapping: mock_card_mapping,
            JiraIntegrationConfig: mock_config,
            JiraIntegrationColumnsMapping: col_mapping,
        },
    )
    mock_httpx_client.get.return_value = MagicMock(
        json=MagicMock(return_value={"transitions": transitions}),
        raise_for_status=MagicMock(),
    )
    mock_httpx_client.post.return_value = MagicMock(raise_for_status=MagicMock())

    provider = JiraIntegrationProvider(mock_session)
    await provider.update_issue_column(mock_project, mock_story, mock_kanban_column)

    expected_url = f"{mock_config.jira_base_url}/rest/api/3/issue/{mock_card_mapping.jira_issue_key}/transitions"
    expected_auth = (mock_config.jira_email, mock_config.jira_api_token)

    mock_httpx_client.get.assert_called_once_with(expected_url, auth=expected_auth)
    mock_httpx_client.post.assert_called_once_with(
        expected_url,
        auth=expected_auth,
        json={"transition": {"id": expected_transition_id}},
    )


@pytest.mark.anyio
async def test_update_issue_column_no_card_mapping(
    mock_session, mock_project, mock_story, mock_kanban_column
):
    configure_session_models(mock_session)
    provider = JiraIntegrationProvider(mock_session)

    with pytest.raises(ValueError, match="Jira issue mapping not found"):
        await provider.update_issue_column(mock_project, mock_story, mock_kanban_column)


@pytest.mark.anyio
async def test_update_issue_column_no_column_mapping(
    mock_session,
    mock_project,
    mock_story,
    mock_config,
    mock_kanban_column,
    mock_card_mapping,
    mock_httpx_client,
):
    configure_session_models(
        mock_session,
        {
            JiraIntegrationCardsMapping: mock_card_mapping,
            JiraIntegrationConfig: mock_config,
        },
    )
    provider = JiraIntegrationProvider(mock_session)

    await provider.update_issue_column(mock_project, mock_story, mock_kanban_column)
    mock_httpx_client.get.assert_not_called()
    mock_httpx_client.post.assert_not_called()


@pytest.mark.anyio
async def test_update_issue_column_no_matching_transition(
    mock_session,
    mock_project,
    mock_story,
    mock_config,
    mock_kanban_column,
    mock_card_mapping,
    mock_column_mapping,
    mock_httpx_client,
):
    configure_session_models(
        mock_session,
        {
            JiraIntegrationCardsMapping: mock_card_mapping,
            JiraIntegrationConfig: mock_config,
            JiraIntegrationColumnsMapping: mock_column_mapping,
        },
    )
    mock_httpx_client.get.return_value = MagicMock(
        json=MagicMock(
            return_value={
                "transitions": [
                    {"id": "11", "name": "Done", "to": {"id": "10003", "name": "Done"}},
                ]
            }
        ),
        raise_for_status=MagicMock(),
    )
    provider = JiraIntegrationProvider(mock_session)

    await provider.update_issue_column(mock_project, mock_story, mock_kanban_column)
    mock_httpx_client.get.assert_called_once()
    mock_httpx_client.post.assert_not_called()


@pytest.mark.anyio
async def test_jira_provider_test_connection(mock_session, mock_config, mock_httpx_client):
    mock_httpx_client.get.return_value = MagicMock(raise_for_status=MagicMock())
    provider = JiraIntegrationProvider(mock_session)
    result = await provider.test_connection(mock_config)
    assert result is True
    mock_httpx_client.get.assert_called_once_with(
        "https://test.atlassian.net/rest/api/3/project/TEST",
        auth=("test@example.com", "token"),
    )


def test_kanban_service_update_issue_column_on_jira_success(
    mock_session, mock_story, mock_kanban_column, mock_project, mock_card_mapping
):
    configure_session_models(
        mock_session,
        {
            Project: mock_project,
            JiraIntegrationCardsMapping: mock_card_mapping,
        },
    )
    service = KanbanService()
    with patch.object(
        JiraIntegrationProvider, "update_issue_column", new_callable=AsyncMock
    ) as mock_update:
        service.update_issue_column_on_jira(mock_session, mock_story, mock_kanban_column)
        mock_update.assert_called_once_with(mock_project, mock_story, mock_kanban_column)


def test_kanban_service_update_issue_column_on_jira_error_handled(
    mock_session, mock_story, mock_kanban_column
):
    configure_session_models(mock_session)
    service = KanbanService()
    service.update_issue_column_on_jira(mock_session, mock_story, mock_kanban_column)


@pytest.mark.anyio
async def test_jira_provider_get_projects(mock_session, mock_httpx_client):
    mock_httpx_client.get.return_value = MagicMock(
        json=MagicMock(return_value=[{"id": "10000", "key": "ESTACK", "name": "E-Stack"}]),
        raise_for_status=MagicMock(),
    )
    provider = JiraIntegrationProvider(mock_session)
    projects = await provider.get_projects("https://test.atlassian.net", "user@test.com", "token")
    assert len(projects) == 1
    assert projects[0]["key"] == "ESTACK"
    mock_httpx_client.get.assert_called_once_with(
        "https://test.atlassian.net/rest/api/3/project",
        headers={"Accept": "application/json"},
        auth=("user@test.com", "token"),
    )


@pytest.mark.anyio
async def test_jira_provider_get_board_columns_with_board_id(mock_session, mock_httpx_client):
    mock_config_resp = MagicMock(
        status_code=200,
        json=MagicMock(
            return_value={
                "columnConfig": {"columns": [{"name": "Done"}]},
                "location": {"projectKey": "ESTACK"},
            }
        ),
        raise_for_status=MagicMock(),
    )
    mock_project_resp = MagicMock(
        status_code=200,
        json=MagicMock(
            return_value={
                "id": "10000",
                "key": "ESTACK",
                "issueTypes": [
                    {"id": "1", "name": "Bug", "isDefault": False},
                    {"id": "2", "name": "Story", "isDefault": True},
                ],
            }
        ),
        raise_for_status=MagicMock(),
    )
    mock_httpx_client.get.side_effect = [mock_config_resp, mock_project_resp]

    provider = JiraIntegrationProvider(mock_session)
    result = await provider.get_board_columns(
        "https://test.atlassian.net", "user@test.com", "token", board_id=42
    )
    assert "columnConfig" in result
    assert result["default_issue_type"] == "Story"
    assert mock_httpx_client.get.call_count == 2


@pytest.mark.anyio
async def test_jira_provider_get_board_columns_with_project_key(mock_session, mock_httpx_client):
    mock_boards_resp = MagicMock(
        status_code=200,
        json=MagicMock(return_value={"values": [{"id": 99, "name": "Board 99"}]}),
        raise_for_status=MagicMock(),
    )
    mock_config_resp = MagicMock(
        status_code=200,
        json=MagicMock(return_value={"columnConfig": {"columns": [{"name": "To Do"}]}}),
        raise_for_status=MagicMock(),
    )
    mock_project_resp = MagicMock(
        status_code=200,
        json=MagicMock(
            return_value={
                "id": "10000",
                "key": "ESTACK",
                "issueTypes": [
                    {"id": "1", "name": "Task"},
                    {"id": "2", "name": "Bug"},
                ],
            }
        ),
        raise_for_status=MagicMock(),
    )
    mock_httpx_client.get.side_effect = [mock_boards_resp, mock_config_resp, mock_project_resp]

    provider = JiraIntegrationProvider(mock_session)
    result = await provider.get_board_columns(
        "https://test.atlassian.net", "user@test.com", "token", project_key_or_id="ESTACK"
    )
    assert "columnConfig" in result
    assert result["default_issue_type"] == "Task"
    assert mock_httpx_client.get.call_count == 3
