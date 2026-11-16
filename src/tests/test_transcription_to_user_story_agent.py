from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

from agents.transcription_to_user_story_agent import run
from database.models import ProjectKanbanColumn, UserStory


class DummyStoryCreated(BaseModel):
    name: str
    description: str
    points: int
    is_done: bool


class DummyStoryUpdated(BaseModel):
    id: str
    name: str
    description: str
    points: int
    is_done: bool


def test_run_raises_value_error_when_no_input_id():
    deps = MagicMock()
    deps.input_id = None

    with pytest.raises(ValueError, match="Input ID is required in deps"):
        run(transcription_formatted="some transcription", deps=deps)


def test_run_logs_warning_when_agent_logger_is_falsy():
    deps = MagicMock()
    deps.input_id = "transcription-1"
    deps.project_id = "proj-1"
    deps.user_id = "user-1"
    deps.agent_logger = None

    mock_agent = MagicMock()
    mock_result = MagicMock()
    mock_result.response.model_name = "gpt-4o"
    mock_result.usage = {"total_tokens": 20}
    mock_result.output.created = []
    mock_result.output.updated = []
    mock_agent.run_sync = MagicMock(return_value=mock_result)

    deps.session.query.return_value.filter.return_value.order_by.return_value.first.return_value = (
        None
    )

    with (
        patch(
            "agents.transcription_to_user_story_agent.create_agent_from_database",
            return_value=mock_agent,
        ),
        patch("agents.transcription_to_user_story_agent.logger") as mock_logger,
    ):
        with pytest.raises(AttributeError):
            run("transcription text", deps)

        mock_logger.warning.assert_called_once_with("No agent logger found in deps")


def test_run_success_with_created_and_updated_stories_and_kanban_column():
    deps = MagicMock()
    deps.input_id = "transcription-1"
    deps.project_id = "proj-1"
    deps.user_id = "user-1"
    deps.agent_logger = MagicMock()

    mock_agent = MagicMock()
    mock_result = MagicMock()
    mock_result.response.model_name = "gpt-4o"
    mock_result.usage = {"prompt_tokens": 50, "completion_tokens": 50}

    mock_result.output.created = [
        DummyStoryCreated(name="Story 1", description="Desc 1", points=3, is_done=False),
        DummyStoryCreated(name="Story 2", description="Desc 2", points=5, is_done=True),
    ]
    mock_result.output.updated = [
        DummyStoryUpdated(
            id="existing-story-1",
            name="Updated Story 1",
            description="Updated Desc 1",
            points=8,
            is_done=True,
        ),
        DummyStoryUpdated(
            id="missing-story-2",
            name="Missing Story 2",
            description="Missing Desc 2",
            points=1,
            is_done=False,
        ),
    ]
    mock_agent.run_sync = MagicMock(return_value=mock_result)

    # Kanban column exists
    mock_column = ProjectKanbanColumn(id="col-1", project_id="proj-1", position=0)
    deps.session.query.return_value.filter.return_value.order_by.return_value.first.return_value = (
        mock_column
    )

    # Existing story in DB for updated_user_stories
    existing_story_in_db = UserStory(
        id="existing-story-1",
        project_id="proj-1",
        name="Old Story 1",
        description="Old Desc 1",
        points=2,
        is_done=False,
    )

    def mock_query(model):
        q = MagicMock()
        if model == ProjectKanbanColumn:
            q.filter.return_value.order_by.return_value.first.return_value = mock_column
        elif model == UserStory:

            def mock_filter_by(id, project_id):
                fq = MagicMock()
                if id == "existing-story-1" and project_id == "proj-1":
                    fq.one_or_none.return_value = existing_story_in_db
                else:
                    fq.one_or_none.return_value = None
                return fq

            q.filter_by.side_effect = mock_filter_by
        return q

    deps.session.query.side_effect = mock_query

    with patch(
        "agents.transcription_to_user_story_agent.create_agent_from_database",
        return_value=mock_agent,
    ) as mock_create_agent:
        fixed_now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)
        with patch("agents.transcription_to_user_story_agent.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            result_stories = run("transcription text", deps)

        mock_create_agent.assert_called_once_with(
            name="transcription_to_story",
            project_id=deps.project_id,
            session=deps.session,
            agent_logger=deps.agent_logger,
        )
        deps.agent_logger.log.assert_called_once_with(
            "Transformando transcrição em user stories..."
        )
        mock_agent.run_sync.assert_called_once()
        deps.agent_logger.finish_agent_run_with_usage.assert_called_once_with(
            "Execução finalizada.", "gpt-4o", {"prompt_tokens": 50, "completion_tokens": 50}
        )

        assert len(result_stories) == 3

        # Created stories checks
        created1 = result_stories[0]
        assert created1.name == "Story 1"
        assert created1.description == "Desc 1"
        assert created1.points == 3
        assert created1.is_done is False
        assert created1.project_id == "proj-1"
        assert created1.user_id == "user-1"
        assert created1.project_kanban_column_id == "col-1"
        assert created1.transcription_id == "transcription-1"
        assert created1.log == "História criada por transcrição"
        assert created1.id is not None

        created2 = result_stories[1]
        assert created2.name == "Story 2"
        assert created2.project_kanban_column_id == "col-1"

        # Updated story checks
        updated1 = result_stories[2]
        assert updated1 == existing_story_in_db
        assert updated1.name == "Updated Story 1"
        assert updated1.description == "Updated Desc 1"
        assert updated1.points == 8
        assert updated1.is_done is True
        assert updated1.user_id == "user-1"
        assert updated1.transcription_id == "transcription-1"
        assert updated1.log == "História atualizada por transcrição"
        assert updated1.timestamp == fixed_now


def test_run_without_kanban_column():
    deps = MagicMock()
    deps.input_id = "transcription-1"
    deps.project_id = "proj-1"
    deps.user_id = "user-1"
    deps.agent_logger = MagicMock()

    mock_agent = MagicMock()
    mock_result = MagicMock()
    mock_result.response.model_name = "gpt-4o"
    mock_result.usage = {"total_tokens": 20}
    mock_result.output.created = [
        DummyStoryCreated(name="Story Without Column", description="Desc", points=1, is_done=False)
    ]
    mock_result.output.updated = []
    mock_agent.run_sync = MagicMock(return_value=mock_result)

    deps.session.query.return_value.filter.return_value.order_by.return_value.first.return_value = (
        None
    )

    with patch(
        "agents.transcription_to_user_story_agent.create_agent_from_database",
        return_value=mock_agent,
    ):
        result_stories = run("transcription text", deps)

        assert len(result_stories) == 1
        assert result_stories[0].project_kanban_column_id is None
