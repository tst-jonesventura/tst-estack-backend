import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks

from database.models import (
    ProjectKanbanColumn,
    User,
    UserStory,
    UserStoryVersion,
)
from routers.crud.user_story_router import (
    USER_STORY_NOT_FOUND_MSG,
    create_user_story,
    delete_user_story,
    get_user_story,
    get_user_story_versions,
    get_user_story_with_criteria,
    list_user_stories,
    update_user_story,
    update_user_story_kanban,
)
from schemas import user_story_schemas


def test_get_user_story_with_criteria_success():
    mock_session = MagicMock()
    story_id = str(uuid.uuid4())
    project_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    story = UserStory(
        id=story_id,
        project_id=project_id,
        name="Test Story",
        description="Description",
        points=3,
        is_done=False,
        timestamp=now,
        gherkin_rules=[],
        tags=[],
        dev_steps=[],
    )
    mock_session.query.return_value.filter.return_value.options.return_value.first.return_value = story

    result = get_user_story_with_criteria(
        project_id=project_id,
        story_id=story_id,
        session=mock_session,
        has_access=True,
    )

    assert result.id == story_id
    assert result.name == "Test Story"


def test_get_user_story_with_criteria_not_found():
    mock_session = MagicMock()
    mock_session.query.return_value.filter.return_value.options.return_value.first.return_value = None

    with pytest.raises(ValueError, match=USER_STORY_NOT_FOUND_MSG):
        get_user_story_with_criteria(
            project_id="proj-1",
            story_id="story-404",
            session=mock_session,
            has_access=True,
        )


def test_get_user_story_versions_success():
    mock_session = MagicMock()
    story_id = str(uuid.uuid4())
    project_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    story = UserStory(id=story_id, project_id=project_id, name="Story")
    mock_session.get.return_value = story

    user = User(
        id="user-1",
        email="user1@example.com",
        is_admin=False,
        is_active=True,
        created_at=now,
    )

    version1 = MagicMock(spec=UserStoryVersion)
    version1.id = story_id
    version1.transaction_id = 1
    version1.name = "Story v1"
    version1.description = "Desc v1"
    version1.points = 1
    version1.is_done = False
    version1.project_id = project_id
    version1.timestamp = now
    version1.user_id = "user-1"
    version1.user = user
    version1.log = "created"
    version1.operation_type = 0
    version1.tags = []
    version1.transcription_id = None

    version2 = MagicMock(spec=UserStoryVersion)
    version2.id = story_id
    version2.transaction_id = 2
    version2.name = "Story v2"
    version2.description = "Desc v2"
    version2.points = 2
    version2.is_done = True
    version2.project_id = project_id
    version2.timestamp = now
    version2.user_id = "user-1"
    version2.user = user
    version2.log = "updated"
    version2.operation_type = 1
    version2.tags = []
    version2.transcription_id = None

    mock_session.query.return_value.filter.return_value.order_by.return_value.all.return_value = [
        version2,
        version1,
    ]

    results = get_user_story_versions(
        project_id=project_id,
        story_id=story_id,
        session=mock_session,
        has_access=True,
    )

    assert len(results) == 2
    assert results[0].name == "Story v2"
    assert results[1].name == "Story v1"


def test_get_user_story_versions_not_found():
    mock_session = MagicMock()
    mock_session.get.return_value = None

    with pytest.raises(ValueError, match=USER_STORY_NOT_FOUND_MSG):
        get_user_story_versions(
            project_id="proj-1",
            story_id="story-404",
            session=mock_session,
            has_access=True,
        )


def test_create_user_story_with_kanban_column():
    mock_session = MagicMock()
    mock_background_tasks = MagicMock(spec=BackgroundTasks)
    mock_user = MagicMock(spec=User, id="user-123")
    mock_column = ProjectKanbanColumn(id="col-1", project_id="proj-1", position=0)

    mock_session.query.return_value.filter.return_value.order_by.return_value.first.return_value = mock_column

    mock_logger = MagicMock(agent_run_id="run-123")
    mock_sync_service = MagicMock()

    now = datetime.now(UTC)
    story_in = user_story_schemas.UserStoryCreate(
        name="New Story",
        description="New Desc",
        points=5,
        is_done=False,
        timestamp=now,
        project_id="proj-1",
    )

    with (
        patch("routers.crud.user_story_router.validate_project_access") as mock_validate_proj,
        patch("routers.crud.user_story_router.SyncService", return_value=mock_sync_service) as mock_sync_cls,
        patch("routers.crud.user_story_router.create_agent_logger", return_value=mock_logger) as mock_create_logger,
        patch("routers.crud.user_story_router.tag_and_commit_in_background") as mock_tag_task,
    ):
        result = create_user_story(
            project_id="proj-1",
            story_in=story_in,
            session=mock_session,
            background_tasks=mock_background_tasks,
            user_data=mock_user,
        )

        mock_validate_proj.assert_called_once_with(mock_session, "proj-1")
        mock_session.add.assert_called_once()
        mock_session.commit.assert_called_once()
        mock_session.refresh.assert_called_once()
        mock_sync_cls.assert_called_once_with(mock_session)

        assert mock_background_tasks.add_task.call_count == 2
        mock_background_tasks.add_task.assert_any_call(
            mock_sync_service.sync_create, "proj-1", result.id
        )
        mock_background_tasks.add_task.assert_any_call(
            mock_tag_task,
            project_id="proj-1",
            user_id="user-123",
            input_id=result.id,
            input_type="user_story",
            agent_run_id="run-123",
        )
        mock_create_logger.assert_called_once_with(
            session=mock_session,
            user_id="user-123",
            project_id="proj-1",
            agent_run_title=f"Criação de tags para user story {result.name}",
            user_story_id=result.id,
            agent_name="labeler",
        )
        assert result.project_kanban_column_id == "col-1"
        assert result.name == "New Story"
        assert result.user_id == "user-123"
        assert result.log == "História criada por usuário"


def test_create_user_story_without_kanban_column():
    mock_session = MagicMock()
    mock_background_tasks = MagicMock(spec=BackgroundTasks)
    mock_user = MagicMock(spec=User, id="user-123")

    mock_session.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
    mock_logger = MagicMock(agent_run_id="run-456")

    now = datetime.now(UTC)
    story_in = user_story_schemas.UserStoryCreate(
        name="Story Without Column",
        description="Desc",
        points=2,
        is_done=False,
        timestamp=now,
        project_id="proj-1",
    )

    with (
        patch("routers.crud.user_story_router.validate_project_access"),
        patch("routers.crud.user_story_router.SyncService"),
        patch("routers.crud.user_story_router.create_agent_logger", return_value=mock_logger),
    ):
        result = create_user_story(
            project_id="proj-1",
            story_in=story_in,
            session=mock_session,
            background_tasks=mock_background_tasks,
            user_data=mock_user,
        )

        assert result.project_kanban_column_id is None


def test_list_user_stories():
    mock_session = MagicMock()
    stories = [
        UserStory(id="s-1", project_id="proj-1", name="Story 1"),
        UserStory(id="s-2", project_id="proj-1", name="Story 2"),
    ]
    mock_session.query.return_value.filter.return_value.all.return_value = stories

    results = list_user_stories(
        project_id="proj-1",
        session=mock_session,
        has_access=True,
    )

    assert results == stories
    mock_session.query.assert_called_once_with(UserStory)


def test_get_user_story_success():
    mock_session = MagicMock()
    story_id = str(uuid.uuid4())
    project_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    story = UserStory(
        id=story_id,
        project_id=project_id,
        name="Single Story",
        description="Desc",
        points=1,
        is_done=True,
        timestamp=now,
        gherkin_rules=[],
        tags=[],
        dev_steps=[],
        kanban_column=None,
        branches=[],
    )
    mock_session.query.return_value.filter.return_value.options.return_value.first.return_value = story

    result = get_user_story(
        project_id=project_id,
        story_id=story_id,
        session=mock_session,
        has_access=True,
    )

    assert result.id == story_id
    assert result.name == "Single Story"


def test_get_user_story_not_found():
    mock_session = MagicMock()
    mock_session.query.return_value.filter.return_value.options.return_value.first.return_value = None

    with pytest.raises(ValueError, match=USER_STORY_NOT_FOUND_MSG):
        get_user_story(
            project_id="proj-1",
            story_id="story-404",
            session=mock_session,
            has_access=True,
        )


def test_update_user_story_success_and_transitions_to_done():
    mock_session = MagicMock()
    mock_background_tasks = MagicMock(spec=BackgroundTasks)
    mock_user = MagicMock(spec=User, id="user-update-1")

    story = UserStory(
        id="story-1",
        project_id="proj-1",
        name="Old Name",
        description="Old Desc",
        points=2,
        is_done=False,
    )
    mock_session.get.return_value = story

    mock_sync_service = MagicMock()
    mock_logger = MagicMock(agent_run_id="run-update-1")

    update_in = user_story_schemas.UserStoryUpdate(
        name="New Name",
        is_done=True,
    )

    with (
        patch("routers.crud.user_story_router.SyncService", return_value=mock_sync_service) as mock_sync_cls,
        patch("routers.crud.user_story_router.create_agent_logger", return_value=mock_logger) as mock_create_logger,
        patch("routers.crud.user_story_router.tag_and_commit_in_background") as mock_tag_task,
        patch("routers.crud.user_story_router.run_story_completion") as mock_completion_task,
    ):
        result = update_user_story(
            project_id="proj-1",
            story_id="story-1",
            story_in=update_in,
            session=mock_session,
            background_tasks=mock_background_tasks,
            user_data=mock_user,
        )

        assert result.name == "New Name"
        assert result.is_done is True
        assert result.user_id == "user-update-1"
        assert result.log == "História atualizada por usuário"
        assert isinstance(result.timestamp, datetime)

        assert mock_session.commit.call_count == 2
        mock_sync_cls.assert_called_once_with(mock_session)
        mock_create_logger.assert_called_once_with(
            session=mock_session,
            user_id="user-update-1",
            project_id="proj-1",
            agent_run_title="Criação de tags para user story New Name",
            user_story_id="story-1",
            agent_name="labeler",
        )

        assert mock_background_tasks.add_task.call_count == 3
        mock_background_tasks.add_task.assert_any_call(
            mock_sync_service.sync_update, "proj-1", "story-1"
        )
        mock_background_tasks.add_task.assert_any_call(
            mock_tag_task,
            project_id="proj-1",
            user_id="user-update-1",
            input_id="story-1",
            input_type="user_story",
            agent_run_id="run-update-1",
        )
        mock_background_tasks.add_task.assert_any_call(
            mock_completion_task,
            "proj-1",
            "story-1",
            "user-update-1",
        )


def test_update_user_story_already_done_does_not_trigger_completion():
    mock_session = MagicMock()
    mock_background_tasks = MagicMock(spec=BackgroundTasks)
    mock_user = MagicMock(spec=User, id="user-update-2")

    story = UserStory(
        id="story-2",
        project_id="proj-1",
        name="Done Story",
        description="Desc",
        points=2,
        is_done=True,
    )
    mock_session.get.return_value = story

    update_in = user_story_schemas.UserStoryUpdate(
        description="Updated Desc only",
    )

    with (
        patch("routers.crud.user_story_router.SyncService"),
        patch("routers.crud.user_story_router.create_agent_logger", return_value=MagicMock(agent_run_id="run-2")),
        patch("routers.crud.user_story_router.run_story_completion") as mock_completion_task,
    ):
        result = update_user_story(
            project_id="proj-1",
            story_id="story-2",
            story_in=update_in,
            session=mock_session,
            background_tasks=mock_background_tasks,
            user_data=mock_user,
        )

        assert result.description == "Updated Desc only"
        assert result.is_done is True
        # Ensure run_story_completion was not scheduled
        for call in mock_background_tasks.add_task.call_args_list:
            assert call[0][0] != mock_completion_task


@pytest.mark.anyio
async def test_delete_user_story_success():
    mock_session = MagicMock()
    story = UserStory(id="story-del-1", project_id="proj-1", name="To Delete")
    mock_session.get.return_value = story

    mock_sync_service = MagicMock()
    mock_sync_service.sync_delete = AsyncMock()

    with patch("routers.crud.user_story_router.SyncService", return_value=mock_sync_service) as mock_sync_cls:
        result = await delete_user_story(
            project_id="proj-1",
            story_id="story-del-1",
            session=mock_session,
            has_access=True,
        )

        mock_sync_cls.assert_called_once_with(mock_session)
        mock_sync_service.sync_delete.assert_awaited_once_with("proj-1", "story-del-1")
        mock_session.delete.assert_called_once_with(story)
        mock_session.commit.assert_called_once()
        assert result == {"detail": "User story deleted successfully"}


@pytest.mark.anyio
async def test_delete_user_story_not_found():
    mock_session = MagicMock()
    mock_session.get.return_value = None

    with pytest.raises(ValueError, match=USER_STORY_NOT_FOUND_MSG):
        await delete_user_story(
            project_id="proj-1",
            story_id="story-del-404",
            session=mock_session,
            has_access=True,
        )


def test_update_user_story_kanban_success():
    mock_session = MagicMock()
    mock_background_tasks = MagicMock()
    mock_user = MagicMock(spec=User, id="user-kanban-1")

    story = UserStory(
        id="story-kanban-1",
        project_id="proj-1",
        name="Kanban Story",
        gherkin_rules=[],
    )
    mock_session.query.return_value.filter.return_value.options.return_value.first.return_value = story

    with (
        patch("routers.crud.user_story_router.validate_user_story_in_project") as mock_validate_story,
        patch("routers.crud.user_story_router.kanban_service.run_kanban_update_flow") as mock_kanban_flow,
    ):
        result = update_user_story_kanban(
            project_id="proj-1",
            story_id="story-kanban-1",
            project_kanban_column_id="col-target-2",
            session=mock_session,
            user_data=mock_user,
            background_tasks=mock_background_tasks,
        )

        mock_validate_story.assert_called_once_with("proj-1", story)
        assert result.user_id == "user-kanban-1"
        assert isinstance(result.timestamp, datetime)
        mock_kanban_flow.assert_called_once_with(
            mock_session,
            mock_background_tasks,
            "proj-1",
            story,
            "col-target-2",
            mock_user,
        )
        mock_session.commit.assert_called_once()