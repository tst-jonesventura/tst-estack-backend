from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from database.models import (
    JiraIntegrationCardsMapping,
    JiraIntegrationColumnsMapping,
    JiraIntegrationConfig,
    Project,
    User,
    UserStory,
)
from services.sync_service import SyncService, _parse_jira_description


@pytest.fixture
def mock_session():
    return MagicMock(spec=Session)


@pytest.fixture
def sync_service(mock_session):
    with patch("services.sync_service.JiraIntegrationProvider", autospec=True):
        service = SyncService(mock_session)
        return service


def test_sync_create_success_new_mapping(sync_service, mock_session):
    # Setup
    project_id = "proj-1"
    story_id = "story-1"
    config_id = "config-1"
    mock_project = MagicMock(spec=Project, id=project_id)
    mock_story = MagicMock(spec=UserStory, id=story_id)
    mock_config = MagicMock(spec=JiraIntegrationConfig, id=config_id, project_id=project_id)

    # Mocking session queries
    def query_side_effect(model):
        mock_query = MagicMock()
        if model == Project:
            mock_query.filter.return_value.first.return_value = mock_project
        elif model == UserStory:
            mock_query.filter.return_value.first.return_value = mock_story
        elif model == JiraIntegrationConfig:
            mock_query.filter.return_value.first.return_value = mock_config
        elif model == JiraIntegrationCardsMapping:
            mock_query.filter.return_value.first.return_value = None
        return mock_query

    mock_session.query.side_effect = query_side_effect

    # Mock provider
    mock_provider = MagicMock()
    mock_provider.create_issue = AsyncMock(return_value="JIRA-123")
    sync_service.providers = [mock_provider]

    # Execute
    sync_service.sync_create(project_id, story_id)

    # Assert
    mock_provider.create_issue.assert_called_once_with(mock_project, mock_story)
    mock_session.add.assert_called()
    mock_session.commit.assert_called()


def test_sync_create_success_existing_mapping(sync_service, mock_session):
    # Setup
    project_id = "proj-1"
    story_id = "story-1"
    config_id = "config-1"
    mock_project = MagicMock(spec=Project, id=project_id)
    mock_story = MagicMock(spec=UserStory, id=story_id)
    mock_config = MagicMock(spec=JiraIntegrationConfig, id=config_id, project_id=project_id)
    mock_mapping = MagicMock(spec=JiraIntegrationCardsMapping)

    def query_side_effect(model):
        mock_query = MagicMock()
        if model == Project:
            mock_query.filter.return_value.first.return_value = mock_project
        elif model == UserStory:
            mock_query.filter.return_value.first.return_value = mock_story
        elif model == JiraIntegrationConfig:
            mock_query.filter.return_value.first.return_value = mock_config
        elif model == JiraIntegrationCardsMapping:
            mock_query.filter.return_value.first.return_value = mock_mapping
        return mock_query

    mock_session.query.side_effect = query_side_effect

    mock_provider = MagicMock()
    mock_provider.create_issue = AsyncMock(return_value="JIRA-123")
    mock_provider.update_issue = AsyncMock()
    sync_service.providers = [mock_provider]

    # Execute
    sync_service.sync_create(project_id, story_id)

    # Assert
    mock_provider.create_issue.assert_called_once()
    mock_provider.update_issue.assert_called_once_with(mock_project, mock_story)
    mock_session.add.assert_not_called()


def test_sync_create_not_found(sync_service, mock_session):
    mock_session.query.return_value.filter.return_value.first.return_value = None

    sync_service.sync_create("any", "any")

    # Providers should not be called if project/story not found
    assert not hasattr(sync_service.providers[0], 'create_issue') or not sync_service.providers[0].create_issue.called


def test_sync_create_no_config(sync_service, mock_session):
    mock_project = MagicMock(spec=Project)
    mock_story = MagicMock(spec=UserStory)

    mock_session.query.return_value.filter.return_value.first.side_effect = [
        mock_project,
        mock_story,
        None,
    ]

    mock_provider = MagicMock()
    mock_provider.create_issue = AsyncMock()
    sync_service.providers = [mock_provider]

    sync_service.sync_create("p", "s")
    mock_provider.create_issue.assert_not_called()


def test_sync_create_exception(sync_service, mock_session):
    mock_project = MagicMock(spec=Project)
    mock_story = MagicMock(spec=UserStory)
    mock_config = MagicMock(spec=JiraIntegrationConfig)

    mock_session.query.return_value.filter.return_value.first.side_effect = [
        mock_project,
        mock_story,
        mock_config,
        None,
    ]

    mock_provider = MagicMock()
    mock_provider.create_issue = AsyncMock(side_effect=Exception("Provider error"))
    sync_service.providers = [mock_provider]

    # Should not raise exception (handled by try-except in code)
    sync_service.sync_create("p", "s")
    mock_provider.create_issue.assert_called_once()


@pytest.mark.anyio
async def test_sync_update_success(sync_service, mock_session):
    mock_project = MagicMock(spec=Project)
    mock_story = MagicMock(spec=UserStory)
    mock_session.query.return_value.filter.return_value.first.side_effect = [mock_project, mock_story]

    mock_provider = MagicMock()
    mock_provider.update_issue = AsyncMock()
    sync_service.providers = [mock_provider]

    await sync_service.sync_update("p", "s")
    mock_provider.update_issue.assert_called_once_with(mock_project, mock_story)


@pytest.mark.anyio
async def test_sync_update_exception(sync_service, mock_session):
    mock_project = MagicMock(spec=Project)
    mock_story = MagicMock(spec=UserStory)
    mock_session.query.return_value.filter.return_value.first.side_effect = [mock_project, mock_story]

    mock_provider = MagicMock()
    mock_provider.update_issue = AsyncMock(side_effect=Exception("Update error"))
    sync_service.providers = [mock_provider]

    await sync_service.sync_update("p", "s")
    mock_provider.update_issue.assert_called_once()


@pytest.mark.anyio
async def test_sync_update_not_found(sync_service, mock_session):
    mock_session.query.return_value.filter.return_value.first.return_value = None

    await sync_service.sync_update("any", "any")

    # Providers should not be called if project/story not found
    assert not hasattr(sync_service.providers[0], 'update_issue') or not sync_service.providers[0].update_issue.called


@pytest.mark.anyio
async def test_sync_delete_success(sync_service, mock_session):
    mock_project = MagicMock(spec=Project)
    mock_story = MagicMock(spec=UserStory)
    mock_session.query.return_value.filter.return_value.first.side_effect = [mock_project, mock_story]

    mock_provider = MagicMock()
    mock_provider.delete_issue = AsyncMock()
    sync_service.providers = [mock_provider]

    await sync_service.sync_delete("proj-1", "story-1")
    mock_provider.delete_issue.assert_called_once_with(mock_project, mock_story)


@pytest.mark.anyio
async def test_sync_delete_not_found(sync_service, mock_session):
    mock_session.query.return_value.filter.return_value.first.return_value = None

    mock_provider = MagicMock()
    mock_provider.delete_issue = AsyncMock()
    sync_service.providers = [mock_provider]

    await sync_service.sync_delete("proj-1", "story-1")
    mock_provider.delete_issue.assert_not_called()


@pytest.mark.anyio
async def test_sync_delete_story_not_found(sync_service, mock_session):
    mock_project = MagicMock(spec=Project)
    mock_session.query.return_value.filter.return_value.first.side_effect = [mock_project, None]

    mock_provider = MagicMock()
    mock_provider.delete_issue = AsyncMock()
    sync_service.providers = [mock_provider]

    await sync_service.sync_delete("proj-1", "story-1")
    mock_provider.delete_issue.assert_not_called()


@pytest.mark.anyio
async def test_sync_delete_exception(sync_service, mock_session):
    mock_project = MagicMock(spec=Project)
    mock_story = MagicMock(spec=UserStory)
    mock_session.query.return_value.filter.return_value.first.side_effect = [mock_project, mock_story]

    mock_provider = MagicMock()
    mock_provider.delete_issue = AsyncMock(side_effect=Exception("Delete error"))
    sync_service.providers = [mock_provider]

    await sync_service.sync_delete("proj-1", "story-1")
    mock_provider.delete_issue.assert_called_once_with(mock_project, mock_story)


def test_sync_service_init():
    session = MagicMock(spec=Session)
    service = SyncService(session)
    assert service.session == session
    assert len(service.providers) == 1


def test_sync_create_story_not_found(sync_service, mock_session):
    mock_project = MagicMock(spec=Project)
    mock_session.query.return_value.filter.return_value.first.side_effect = [mock_project, None]

    sync_service.sync_create("p", "s")

    # Provider should not be called if story not found
    assert not hasattr(sync_service.providers[0], 'create_issue') or not sync_service.providers[0].create_issue.called


@pytest.mark.anyio
async def test_handle_webhook_unknown_event(sync_service):
    payload = {"webhookEvent": "jira:unknown_event", "issue": {"key": "K"}}
    # Should not raise exception or call any internal methods
    sync_service.handle_webhook(payload)
    sync_service.session.query.assert_not_called()


@pytest.mark.anyio
async def test_handle_webhook_no_event(sync_service):
    payload = {"issue": {"key": "K"}}
    sync_service.handle_webhook(payload)
    sync_service.session.query.assert_not_called()


@pytest.mark.anyio
async def test_handle_webhook_no_issue_key(sync_service):
    payload = {"webhookEvent": "jira:issue_updated", "issue": {}}
    sync_service.handle_webhook(payload)
    sync_service.session.query.assert_not_called()


@pytest.mark.anyio
async def test_handle_webhook_update_no_mapping_does_nothing(sync_service, mock_session):
    # Test case updated because sync_service.py no longer triggers creation on update event
    # when the story is not found in DB.
    payload = {"webhookEvent": "jira:issue_updated", "issue": {"key": "JIRA-NEW"}}
    mock_session.query.return_value.filter.return_value.first.return_value = None

    with patch.object(sync_service, "_create_user_story_from_jira_issue") as mock_create:
        sync_service.handle_webhook(payload)
        mock_create.assert_not_called()


@pytest.mark.anyio
async def test_handle_webhook_update_story_success(sync_service, mock_session):
    payload = {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "key": "JIRA-1",
            "fields": {
                "summary": "New Name",
                "description": "New Desc",
                "resolution": {"name": "Done"},
                "status": {"id": "10001"}
            }
        }
    }
    mock_mapping = MagicMock(spec=JiraIntegrationCardsMapping, user_story_id="story-1")
    mock_story = MagicMock(spec=UserStory, id="story-1", is_done=False)
    mock_column_mapping = MagicMock(spec=JiraIntegrationColumnsMapping, project_kanban_column_id="col-123")

    def query_side_effect(model):
        mock_query = MagicMock()
        if model == JiraIntegrationCardsMapping:
            mock_query.filter.return_value.first.return_value = mock_mapping
        elif model == UserStory:
            mock_query.filter.return_value.first.return_value = mock_story
        elif model == JiraIntegrationColumnsMapping:
            mock_query.filter.return_value.first.return_value = mock_column_mapping
        return mock_query

    mock_session.query.side_effect = query_side_effect

    sync_service.handle_webhook(payload)

    assert mock_story.name == "New Name"
    assert mock_story.is_done is True
    assert mock_story.project_kanban_column_id == "col-123"
    mock_session.add.assert_called_with(mock_story)
    mock_session.commit.assert_called_once()


@pytest.mark.anyio
async def test_handle_webhook_update_story_no_resolution(sync_service, mock_session):
    payload = {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "key": "JIRA-1",
            "fields": {
                "summary": "Update Name",
                "description": "Update Desc",
                "resolution": None,
                "status": {"id": "10001"}
            }
        }
    }
    mock_mapping = MagicMock(spec=JiraIntegrationCardsMapping, user_story_id="story-1")
    mock_story = MagicMock(spec=UserStory, id="story-1", is_done=True)  # Start as True to see it change to False
    mock_column_mapping = MagicMock(spec=JiraIntegrationColumnsMapping, project_kanban_column_id="col-123")

    def query_side_effect(model):
        mock_query = MagicMock()
        if model == JiraIntegrationCardsMapping:
            mock_query.filter.return_value.first.return_value = mock_mapping
        elif model == UserStory:
            mock_query.filter.return_value.first.return_value = mock_story
        elif model == JiraIntegrationColumnsMapping:
            mock_query.filter.return_value.first.return_value = mock_column_mapping
        return mock_query

    mock_session.query.side_effect = query_side_effect

    sync_service.handle_webhook(payload)

    assert mock_story.is_done is False
    mock_session.commit.assert_called_once()


@pytest.mark.anyio
async def test_handle_webhook_update_story_no_status(sync_service, mock_session):
    payload = {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "key": "JIRA-1",
            "fields": {
                "summary": "Update Name",
                "description": "Update Desc",
                "status": None
            }
        }
    }
    mock_mapping = MagicMock(spec=JiraIntegrationCardsMapping, user_story_id="story-1")
    mock_story = MagicMock(spec=UserStory, id="story-1")

    def query_side_effect(model):
        mock_query = MagicMock()
        if model == JiraIntegrationCardsMapping:
            mock_query.filter.return_value.first.return_value = mock_mapping
        elif model == UserStory:
            mock_query.filter.return_value.first.return_value = mock_story
        return mock_query

    mock_session.query.side_effect = query_side_effect

    sync_service.handle_webhook(payload)

    assert mock_story.log == "Updated from Jira webhook"
    mock_session.commit.assert_called_once()


@pytest.mark.anyio
async def test_handle_webhook_update_story_status_no_mapping(sync_service, mock_session):
    payload = {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "key": "JIRA-1",
            "fields": {
                "summary": "Update Name",
                "status": {"id": "999"}
            }
        }
    }
    mock_mapping = MagicMock(spec=JiraIntegrationCardsMapping, user_story_id="story-1")
    mock_story = MagicMock(spec=UserStory, id="story-1")

    def query_side_effect(model):
        mock_query = MagicMock()
        if model == JiraIntegrationCardsMapping:
            mock_query.filter.return_value.first.return_value = mock_mapping
        elif model == UserStory:
            mock_query.filter.return_value.first.return_value = mock_story
        elif model == JiraIntegrationColumnsMapping:
            mock_query.filter.return_value.first.return_value = None
        return mock_query

    mock_session.query.side_effect = query_side_effect

    sync_service.handle_webhook(payload)

    assert mock_story.log == "Updated from Jira webhook"
    mock_session.commit.assert_called_once()


@pytest.mark.anyio
async def test_handle_webhook_story_not_found(sync_service, mock_session):
    payload = {"webhookEvent": "jira:issue_updated", "issue": {"key": "JIRA-1"}}
    mock_mapping = MagicMock(spec=JiraIntegrationCardsMapping, user_story_id="story-1")

    def query_side_effect(model):
        mock_query = MagicMock()
        if model == JiraIntegrationCardsMapping:
            mock_query.filter.return_value.first.return_value = mock_mapping
        elif model == UserStory:
            mock_query.filter.return_value.first.return_value = None
        return mock_query

    mock_session.query.side_effect = query_side_effect

    with pytest.raises(ValueError, match="Story story-1 not found"):
        sync_service.handle_webhook(payload)


@pytest.mark.anyio
async def test_handle_webhook_create_new_story(sync_service, mock_session):
    payload = {"issue": {"key": "JIRA-NEW", "fields": {"project": {"id": "p-id", "key": "PK"}}}}

    # Mock mapping query returns None to trigger creation
    mock_session.query.return_value.filter.return_value.first.return_value = None

    with patch.object(sync_service, "_create_user_story_from_jira_issue") as mock_create:
        payload["webhookEvent"] = "jira:issue_created"
        sync_service.handle_webhook(payload)
        mock_create.assert_called_once()


@pytest.mark.anyio
async def test_handle_webhook_exception(sync_service, mock_session):
    mock_session.query.side_effect = Exception("DB Error")
    with pytest.raises(Exception, match="DB Error"):
        sync_service.handle_webhook({"webhookEvent": "jira:issue_updated", "issue": {"key": "K"}})


@pytest.mark.anyio
async def test_handle_webhook_delete_story_success(sync_service, mock_session):
    payload = {"webhookEvent": "jira:issue_deleted", "issue": {"key": "JIRA-1"}}
    mock_mapping = MagicMock(spec=JiraIntegrationCardsMapping, user_story_id="story-1")
    mock_story = MagicMock(spec=UserStory, id="story-1")

    def query_side_effect(model):
        mock_query = MagicMock()
        if model == JiraIntegrationCardsMapping:
            mock_query.filter.return_value.first.return_value = mock_mapping
        elif model == UserStory:
            mock_query.filter.return_value.first.return_value = mock_story
        return mock_query

    mock_session.query.side_effect = query_side_effect

    sync_service.handle_webhook(payload)

    mock_session.delete.assert_called_with(mock_story)
    mock_session.commit.assert_called_once()


@pytest.mark.anyio
async def test_handle_webhook_delete_story_not_found(sync_service, mock_session):
    payload = {"webhookEvent": "jira:issue_deleted", "issue": {"key": "JIRA-1"}}
    mock_mapping = MagicMock(spec=JiraIntegrationCardsMapping, user_story_id="story-1")

    def query_side_effect(model):
        mock_query = MagicMock()
        if model == JiraIntegrationCardsMapping:
            mock_query.filter.return_value.first.return_value = mock_mapping
        elif model == UserStory:
            mock_query.filter.return_value.first.return_value = None
        return mock_query

    mock_session.query.side_effect = query_side_effect

    with pytest.raises(ValueError, match="User story not found for deletion"):
        sync_service.handle_webhook(payload)


@pytest.mark.anyio
async def test_handle_webhook_delete_story_no_mapping_does_nothing(sync_service, mock_session):
    payload = {"webhookEvent": "jira:issue_deleted", "issue": {"key": "JIRA-NOT-FOUND"}}
    mock_session.query.return_value.filter.return_value.first.return_value = None

    sync_service.handle_webhook(payload)

    mock_session.delete.assert_not_called()


@pytest.mark.anyio
async def test_create_user_story_from_jira_issue_success(sync_service, mock_session):
    issue = {
        "fields": {
            "project": {"id": "100", "key": "PROJ"},
            "summary": "Title",
            "description": "Desc",
            "customfield_10016": 5,
        }
    }
    key = "PROJ-1"

    mock_config = MagicMock(spec=JiraIntegrationConfig, project_id="p-123", id="c-123")
    mock_user = MagicMock(spec=User, id="u-123")

    def query_side_effect(model):
        mock_query = MagicMock()
        if model == JiraIntegrationConfig:
            mock_query.filter.return_value.filter.return_value.first.return_value = mock_config
        elif model == JiraIntegrationCardsMapping:
            mock_query.filter.return_value.first.return_value = None
        elif model == User:
            mock_query.filter.return_value.first.return_value = mock_user
        return mock_query

    mock_session.query.side_effect = query_side_effect

    result = sync_service._create_user_story_from_jira_issue(issue, key)

    assert result is not None
    assert result.name == "Title"
    assert result.description == "Desc"
    assert result.points == 5
    assert result.project_id == "p-123"
    assert result.user_id == "u-123"
    assert mock_session.add.call_count == 2
    mock_session.flush.assert_called_once()
    mock_session.commit.assert_called_once()


@pytest.mark.anyio
async def test_create_user_story_from_jira_issue_already_exists(sync_service, mock_session):
    issue = {
        "fields": {
            "project": {"id": "100", "key": "PROJ"},
            "summary": "Title",
            "description": "Desc",
        }
    }
    key = "PROJ-1"
    mock_config = MagicMock(spec=JiraIntegrationConfig, project_id="p-123")
    mock_mapping = MagicMock(spec=JiraIntegrationCardsMapping)

    def query_side_effect(model):
        mock_query = MagicMock()
        if model == JiraIntegrationConfig:
            mock_query.filter.return_value.filter.return_value.first.return_value = mock_config
        elif model == JiraIntegrationCardsMapping:
            mock_query.filter.return_value.first.return_value = mock_mapping
        return mock_query

    mock_session.query.side_effect = query_side_effect

    result = sync_service._create_user_story_from_jira_issue(issue, key)
    assert result is None
    mock_session.commit.assert_not_called()


@pytest.mark.anyio
async def test_create_user_story_from_jira_issue_no_config(sync_service, mock_session):
    issue = {"fields": {"project": {"id": "1", "key": "K"}}}
    mock_session.query.return_value.filter.return_value.filter.return_value.first.return_value = None

    with pytest.raises(ValueError, match="No JiraIntegrationConfig found"):
        sync_service._create_user_story_from_jira_issue(issue, "K")


@pytest.mark.anyio
async def test_create_user_story_from_jira_issue_no_user(sync_service, mock_session):
    issue = {"fields": {"project": {"id": "1", "key": "K"}}}
    mock_config = MagicMock(spec=JiraIntegrationConfig)

    def query_side_effect(model):
        mock_query = MagicMock()
        if model == JiraIntegrationConfig:
            mock_query.filter.return_value.filter.return_value.first.return_value = mock_config
        elif model in (JiraIntegrationCardsMapping, User):
            mock_query.filter.return_value.first.return_value = None
        return mock_query

    mock_session.query.side_effect = query_side_effect

    with pytest.raises(ValueError, match="User with email 'e-stack@elumini-it.com' not found"):
        sync_service._create_user_story_from_jira_issue(issue, "K")


@pytest.mark.anyio
async def test_create_user_story_from_jira_issue_points_none(sync_service, mock_session):
    issue = {
        "fields": {
            "project": {"id": "100", "key": "PROJ"},
            "summary": "Title",
            "description": "Desc",
            "customfield_10016": None,
        }
    }
    mock_config = MagicMock(spec=JiraIntegrationConfig, project_id="p-123", id="c-123")
    mock_user = MagicMock(spec=User, id="u-123")

    def query_side_effect(model):
        mock_query = MagicMock()
        if model == JiraIntegrationConfig:
            mock_query.filter.return_value.filter.return_value.first.return_value = mock_config
        elif model == JiraIntegrationCardsMapping:
            mock_query.filter.return_value.first.return_value = None
        elif model == User:
            mock_query.filter.return_value.first.return_value = mock_user
        return mock_query

    mock_session.query.side_effect = query_side_effect

    result = sync_service._create_user_story_from_jira_issue(issue, "K")
    assert result is not None
    assert result.points == 0


def test_parse_jira_description_adf(sync_service):
    adf = {
        "content": [
            {
                "content": [
                    {"text": "Hello World"}
                ]
            }
        ]
    }
    assert _parse_jira_description(adf) == "Hello World"


def test_parse_jira_description_adf_empty(sync_service):
    assert _parse_jira_description({"content": []}) == ""
    assert _parse_jira_description({"content": [{}]}) == ""
    assert _parse_jira_description({"content": [{"content": []}]}) == ""
    assert _parse_jira_description({}) == ""


def test_parse_jira_description_string(sync_service):
    assert _parse_jira_description("Plain text") == "Plain text"
    assert _parse_jira_description(None) == ""
