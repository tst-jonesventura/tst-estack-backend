from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from database.database import get_session
from database.models import User
from routers.agents.transcription_to_user_story_router import (
    router,
    transcription_to_user_stories,
    transcription_to_user_stories_flow,
)
from schemas.agent_schemas import ExecutionIdPublic
from utils.bearer_auth import check_project_access


def test_transcription_to_user_stories_flow_with_stories():
    mock_session = MagicMock()
    mock_session_cm = MagicMock()
    mock_session_cm.__enter__.return_value = mock_session
    mock_session_cm.__exit__.return_value = None

    story1 = MagicMock(id="story-1")
    story2 = MagicMock(id="story-2")
    user_stories = [story1, story2]

    tags_story1 = [MagicMock(id="tag-1")]
    tags_story2 = [MagicMock(id="tag-2"), MagicMock(id="tag-3")]

    mock_sync_service = MagicMock()

    with (
        patch(
            "routers.agents.transcription_to_user_story_router.create_session",
            return_value=mock_session_cm,
        ),
        patch(
            "routers.agents.transcription_to_user_story_router.SyncService",
            return_value=mock_sync_service,
        ) as mock_sync_cls,
        patch(
            "routers.agents.transcription_to_user_story_router.AgentLogger"
        ) as mock_logger_cls,
        patch(
            "routers.agents.transcription_to_user_story_router.AgentDeps"
        ) as mock_deps_cls,
        patch(
            "routers.agents.transcription_to_user_story_router.wait_for_sequential_slot"
        ) as mock_wait_slot,
        patch(
            "routers.agents.transcription_to_user_story_router.transcription_to_user_story_agent.run",
            return_value=user_stories,
        ) as mock_agent_run,
        patch(
            "routers.agents.transcription_to_user_story_router.labeler_agent.run",
            side_effect=[tags_story1, tags_story2],
        ) as mock_labeler_run,
    ):
        mock_deps_instance = MagicMock()
        mock_deps_cls.return_value = mock_deps_instance
        mock_agent_logger_instance = MagicMock()
        mock_logger_cls.return_value = mock_agent_logger_instance

        transcription_to_user_stories_flow(
            project_id="proj-123",
            user_id="user-456",
            transcription_formatted="formatted transcription text",
            document_transcription_id="transcription-789",
            agent_run_id="run-001",
        )

        mock_sync_cls.assert_called_once_with(mock_session)
        mock_logger_cls.assert_called_once_with("run-001")
        mock_deps_cls.assert_called_once_with(
            project_id="proj-123",
            agent_logger=mock_agent_logger_instance,
            user_id="user-456",
            input_id="transcription-789",
            input_type="transcription",
            session=mock_session,
        )
        mock_wait_slot.assert_called_once_with(
            "proj-123", "transcription_to_story", "run-001"
        )
        mock_agent_run.assert_called_once_with(
            "formatted transcription text", mock_deps_instance
        )

        # First add_all for user stories and commit
        mock_session.add_all.assert_any_call(user_stories)
        assert mock_session.commit.call_count == 3  # initial + 2 stories

        # Sync service calls
        assert mock_sync_service.sync_create.call_count == 2
        mock_sync_service.sync_create.assert_any_call("proj-123", "story-1")
        mock_sync_service.sync_create.assert_any_call("proj-123", "story-2")

        # Labeler agent calls
        mock_session.add_all.assert_any_call(tags_story1)
        mock_session.add_all.assert_any_call(tags_story2)

        assert mock_deps_instance.input_id == "story-2"
        assert mock_deps_instance.input_type == "user_story"


def test_transcription_to_user_stories_flow_without_stories():
    mock_session = MagicMock()
    mock_session_cm = MagicMock()
    mock_session_cm.__enter__.return_value = mock_session
    mock_session_cm.__exit__.return_value = None

    mock_sync_service = MagicMock()

    with (
        patch(
            "routers.agents.transcription_to_user_story_router.create_session",
            return_value=mock_session_cm,
        ),
        patch(
            "routers.agents.transcription_to_user_story_router.SyncService",
            return_value=mock_sync_service,
        ),
        patch(
            "routers.agents.transcription_to_user_story_router.AgentLogger"
        ),
        patch(
            "routers.agents.transcription_to_user_story_router.AgentDeps"
        ),
        patch(
            "routers.agents.transcription_to_user_story_router.wait_for_sequential_slot"
        ),
        patch(
            "routers.agents.transcription_to_user_story_router.transcription_to_user_story_agent.run",
            return_value=[],
        ),
    ):
        transcription_to_user_stories_flow(
            project_id="proj-123",
            user_id="user-456",
            transcription_formatted="formatted transcription text",
            document_transcription_id="transcription-789",
            agent_run_id="run-001",
        )

        mock_session.add_all.assert_called_once_with([])
        mock_session.commit.assert_called_once()
        mock_sync_service.sync_create.assert_not_called()


@pytest.mark.anyio
async def test_transcription_to_user_stories_route_function():
    mock_session = MagicMock()
    mock_background_tasks = MagicMock()
    mock_user = MagicMock(spec=User, id="user-123")

    mock_agent_logger = MagicMock()
    mock_agent_logger.agent_run_id = "agent-run-999"

    with (
        patch(
            "routers.agents.transcription_to_user_story_router.format_transcription",
            return_value="formatted-content",
        ) as mock_format,
        patch(
            "routers.agents.transcription_to_user_story_router.create_agent_logger",
            return_value=mock_agent_logger,
        ) as mock_create_logger,
    ):
        result = await transcription_to_user_stories(
            project_id="proj-456",
            document_transcription_id="doc-trans-789",
            session=mock_session,
            background_tasks=mock_background_tasks,
            user_data=mock_user,
        )

        mock_format.assert_called_once_with(
            mock_session, "proj-456", "doc-trans-789"
        )
        mock_create_logger.assert_called_once_with(
            session=mock_session,
            user_id="user-123",
            project_id="proj-456",
            agent_run_title="Transcrição para User Story doc-trans-789",
            queue_key="transcription_to_story",
            agent_name="transcription_to_story",
        )
        mock_background_tasks.add_task.assert_called_once_with(
            transcription_to_user_stories_flow,
            project_id="proj-456",
            user_id="user-123",
            transcription_formatted="formatted-content",
            document_transcription_id="doc-trans-789",
            agent_run_id="agent-run-999",
        )
        assert isinstance(result, ExecutionIdPublic)
        assert result.execution_id == "agent-run-999"


def test_transcription_to_user_stories_http_endpoint():
    app = FastAPI()
    app.include_router(router)

    mock_session = MagicMock(spec=Session)
    mock_user = MagicMock(spec=User, id="user-http-456")

    app.dependency_overrides[get_session] = lambda: mock_session
    app.dependency_overrides[check_project_access] = lambda: mock_user

    mock_agent_logger = MagicMock()
    mock_agent_logger.agent_run_id = "run-http-123"

    client = TestClient(app)

    with (
        patch(
            "routers.agents.transcription_to_user_story_router.format_transcription",
            return_value="mocked-formatted-transcription",
        ) as mock_format,
        patch(
            "routers.agents.transcription_to_user_story_router.create_agent_logger",
            return_value=mock_agent_logger,
        ) as mock_create_logger,
        patch(
            "routers.agents.transcription_to_user_story_router.transcription_to_user_stories_flow",
            new_callable=AsyncMock,
        ) as mock_flow,
    ):
        response = client.post("/proj-100/doc-trans-200")

        assert response.status_code == 200
        data = response.json()
        assert data == {"execution_id": "run-http-123"}

        mock_format.assert_called_once_with(mock_session, "proj-100", "doc-trans-200")
        mock_create_logger.assert_called_once_with(
            session=mock_session,
            user_id="user-http-456",
            project_id="proj-100",
            agent_run_title="Transcrição para User Story doc-trans-200",
            queue_key="transcription_to_story",
            agent_name="transcription_to_story",
        )
        mock_flow.assert_awaited_once_with(
            project_id="proj-100",
            user_id="user-http-456",
            transcription_formatted="mocked-formatted-transcription",
            document_transcription_id="doc-trans-200",
            agent_run_id="run-http-123",
        )
