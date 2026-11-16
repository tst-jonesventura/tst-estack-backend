from unittest.mock import MagicMock, patch

import pytest
from fastapi import BackgroundTasks

from agents.story_completion_agent import run_story_completion
from database.models import (
    JiraIntegrationCardsMapping,
    Project,
    ProjectKanbanColumn,
    ProjectKanbanColumnRule,
    User,
    UserStory,
)
from services.kanban_service import (
    KanbanService,
    _run_handler_in_background,
    kanban_service,
)


@pytest.fixture
def service():
    return KanbanService()


@pytest.fixture
def mock_session():
    return MagicMock()


@pytest.fixture
def mock_background_tasks():
    return MagicMock(spec=BackgroundTasks)


@pytest.fixture
def sample_user():
    user = MagicMock(spec=User)
    user.id = "user-123"
    user.name = "Test User"
    return user


@pytest.fixture
def sample_story():
    story = MagicMock(spec=UserStory)
    story.id = "story-123"
    story.project_id = "project-123"
    story.user_id = "user-123"
    story.project_kanban_column_id = "col-old"
    story.is_done = False
    story.log = "Initial log"
    old_column = MagicMock(spec=ProjectKanbanColumn)
    old_column.id = "col-old"
    old_column.name = "In Progress"
    old_column.position = 1
    old_column.project_id = "project-123"
    story.kanban_column = old_column
    return story


@pytest.fixture
def sample_columns():
    col0 = MagicMock(spec=ProjectKanbanColumn, id="col-0", position=0, project_id="project-123")
    col0.name = "To Do"
    col1 = MagicMock(spec=ProjectKanbanColumn, id="col-1", position=1, project_id="project-123")
    col1.name = "In Progress"
    col2 = MagicMock(spec=ProjectKanbanColumn, id="col-2", position=2, project_id="project-123")
    col2.name = "Done"
    return [col0, col1, col2]


# ==============================================================================
# Initialization & Handler Keys
# ==============================================================================


def test_kanban_service_init():
    service = KanbanService()
    assert "create_gherkin" in service.handlers
    assert "create_tdd_unit_tests" in service.handlers
    assert "create_user_story_dev_steps" in service.handlers
    assert "create_tdd_code" in service.handlers


def test_get_handler_keys(service):
    keys = service.get_handler_keys()
    assert set(keys) == {
        "create_gherkin",
        "create_tdd_unit_tests",
        "create_user_story_dev_steps",
        "create_tdd_code",
    }


def test_global_kanban_service_instance():
    assert isinstance(kanban_service, KanbanService)


# ==============================================================================
# update_kanban_column_and_its_log
# ==============================================================================


def test_update_kanban_column_and_its_log_with_old_column(service, mock_session, sample_story):
    old_col = MagicMock(spec=ProjectKanbanColumn)
    old_col.name = "Backlog"
    new_col = MagicMock(spec=ProjectKanbanColumn, id="col-new")
    new_col.name = "Ready for Dev"
    mock_session.get.return_value = old_col

    service.update_kanban_column_and_its_log(mock_session, sample_story, new_col)

    mock_session.get.assert_called_once_with(ProjectKanbanColumn, "col-old")
    assert sample_story.kanban_column == new_col
    assert sample_story.log == "História atualizada por usuário. Status alterado de 'Backlog' para 'Ready for Dev' "


def test_update_kanban_column_and_its_log_without_old_column(service, mock_session, sample_story):
    new_col = MagicMock(spec=ProjectKanbanColumn, id="col-new")
    new_col.name = "Ready for Dev"
    mock_session.get.return_value = None
    sample_story.log = "Existing log"

    service.update_kanban_column_and_its_log(mock_session, sample_story, new_col)

    mock_session.get.assert_called_once_with(ProjectKanbanColumn, "col-old")
    assert sample_story.kanban_column == new_col
    assert sample_story.log == "Existing log"


# ==============================================================================
# mark_done_if_last_column
# ==============================================================================


def test_mark_done_if_last_column_when_in_last_column(
    service, mock_session, mock_background_tasks, sample_story, sample_user
):
    # 3 total columns (positions 0, 1, 2). Story is in column position 2 (last column).
    sample_story.kanban_column.position = 2
    sample_story.log = "Log before done"

    mock_query = mock_session.query.return_value
    mock_filter = mock_query.filter.return_value
    mock_filter.count.return_value = 3

    service.mark_done_if_last_column(
        mock_session, "project-123", sample_story, mock_background_tasks, sample_user
    )

    assert sample_story.is_done is True
    assert sample_story.log == "Log before done e marcada como concluída."
    mock_background_tasks.add_task.assert_called_once_with(
        run_story_completion,
        "project-123",
        sample_story.id,
        sample_user.id,
    )


def test_mark_done_if_last_column_when_not_in_last_column(
    service, mock_session, mock_background_tasks, sample_story, sample_user
):
    # 3 total columns (positions 0, 1, 2). Story is in position 1.
    sample_story.kanban_column.position = 1
    sample_story.is_done = False
    sample_story.log = "Log not done"

    mock_query = mock_session.query.return_value
    mock_filter = mock_query.filter.return_value
    mock_filter.count.return_value = 3

    service.mark_done_if_last_column(
        mock_session, "project-123", sample_story, mock_background_tasks, sample_user
    )

    assert sample_story.is_done is False
    assert sample_story.log == "Log not done"
    mock_background_tasks.add_task.assert_not_called()


# ==============================================================================
# run_kanban_columns_rules
# ==============================================================================


def test_run_kanban_columns_rules_empty(
    service, mock_session, mock_background_tasks, sample_story, sample_user
):
    mock_session.query.return_value.filter.return_value.order_by.return_value.all.return_value = []

    service.run_kanban_columns_rules(
        mock_session, mock_background_tasks, sample_story, sample_user
    )

    mock_background_tasks.add_task.assert_not_called()


def test_run_kanban_columns_rules_trigger_blocking(
    service, mock_session, mock_background_tasks, sample_story, sample_user
):
    mock_handler = MagicMock()
    service.handlers["create_gherkin"] = mock_handler

    rule = MagicMock(spec=ProjectKanbanColumnRule)
    rule.id = "rule-1"
    rule.action_type = "trigger"
    rule.handler_key = "create_gherkin"
    rule.is_blocking = True
    rule.order = 1

    mock_session.query.return_value.filter.return_value.order_by.return_value.all.return_value = [rule]
    sample_story.log = "Log"

    service.run_kanban_columns_rules(
        mock_session, mock_background_tasks, sample_story, sample_user
    )

    assert "iniciada a execução do agent create_gherkin." in sample_story.log
    mock_handler.assert_called_once_with(
        mock_session,
        sample_story.kanban_column.project_id,
        sample_user,
        sample_story,
    )
    mock_background_tasks.add_task.assert_not_called()


def test_run_kanban_columns_rules_trigger_non_blocking(
    service, mock_session, mock_background_tasks, sample_story, sample_user
):
    mock_handler = MagicMock()
    service.handlers["create_tdd_code"] = mock_handler

    rule = MagicMock(spec=ProjectKanbanColumnRule)
    rule.id = "rule-2"
    rule.action_type = "TRIGGER"
    rule.handler_key = "create_tdd_code"
    rule.is_blocking = False
    rule.order = 1

    mock_session.query.return_value.filter.return_value.order_by.return_value.all.return_value = [rule]
    sample_story.log = "Log"

    service.run_kanban_columns_rules(
        mock_session, mock_background_tasks, sample_story, sample_user
    )

    assert "iniciada a execução do agent create_tdd_code." in sample_story.log
    mock_handler.assert_not_called()
    mock_background_tasks.add_task.assert_called_once_with(
        _run_handler_in_background,
        mock_handler,
        sample_story.kanban_column.project_id,
        sample_user.id,
        sample_story.id,
    )


def test_run_kanban_columns_rules_trigger_unknown_handler_key(
    service, mock_session, mock_background_tasks, sample_story, sample_user
):
    mock_handler = MagicMock()
    service.handlers["create_gherkin"] = mock_handler

    rule_invalid = MagicMock(spec=ProjectKanbanColumnRule)
    rule_invalid.id = "rule-invalid"
    rule_invalid.action_type = "trigger"
    rule_invalid.handler_key = "non_existent_handler"
    rule_invalid.is_blocking = True

    rule_valid = MagicMock(spec=ProjectKanbanColumnRule)
    rule_valid.id = "rule-valid"
    rule_valid.action_type = "trigger"
    rule_valid.handler_key = "create_gherkin"
    rule_valid.is_blocking = True

    mock_session.query.return_value.filter.return_value.order_by.return_value.all.return_value = [
        rule_invalid,
        rule_valid,
    ]
    sample_story.log = "Log"

    service.run_kanban_columns_rules(
        mock_session, mock_background_tasks, sample_story, sample_user
    )

    # Invalid handler skipped, valid handler executed
    assert "iniciada a execução do agent non_existent_handler." not in sample_story.log
    assert "iniciada a execução do agent create_gherkin." in sample_story.log
    mock_handler.assert_called_once_with(
        mock_session,
        sample_story.kanban_column.project_id,
        sample_user,
        sample_story,
    )


def test_run_kanban_columns_rules_validation_type(
    service, mock_session, mock_background_tasks, sample_story, sample_user
):
    rule = MagicMock(spec=ProjectKanbanColumnRule)
    rule.id = "rule-val"
    rule.action_type = "validation"
    rule.handler_key = "create_gherkin"

    mock_session.query.return_value.filter.return_value.order_by.return_value.all.return_value = [rule]

    service.run_kanban_columns_rules(
        mock_session, mock_background_tasks, sample_story, sample_user
    )

    mock_background_tasks.add_task.assert_not_called()


def test_run_kanban_columns_rules_unknown_action_type(
    service, mock_session, mock_background_tasks, sample_story, sample_user
):
    rule = MagicMock(spec=ProjectKanbanColumnRule)
    rule.id = "rule-unknown"
    rule.action_type = "unknown_type"
    rule.handler_key = "create_gherkin"

    mock_session.query.return_value.filter.return_value.order_by.return_value.all.return_value = [rule]

    service.run_kanban_columns_rules(
        mock_session, mock_background_tasks, sample_story, sample_user
    )

    mock_background_tasks.add_task.assert_not_called()


# ==============================================================================
# update_issue_column_on_jira
# ==============================================================================


def test_update_issue_column_on_jira_no_project_id(service, mock_session):
    story_without_proj = MagicMock(spec=UserStory)
    del story_without_proj.project_id

    service.update_issue_column_on_jira(mock_session, story_without_proj, None)
    mock_session.query.assert_not_called()


def test_update_issue_column_on_jira_mapping_not_found(service, mock_session, sample_story):
    new_col = MagicMock(spec=ProjectKanbanColumn, id="col-new")
    mock_session.query.return_value.filter.return_value.first.return_value = None

    service.update_issue_column_on_jira(mock_session, sample_story, new_col)

    mock_session.query.assert_called_once_with(JiraIntegrationCardsMapping)


def test_update_issue_column_on_jira_project_id_from_column(service, mock_session):
    story_without_proj = MagicMock(spec=UserStory, id="story-123")
    del story_without_proj.project_id
    new_col = MagicMock(spec=ProjectKanbanColumn, project_id="proj-from-col")

    mock_mapping = MagicMock(spec=JiraIntegrationCardsMapping)

    def mock_query(model):
        query_mock = MagicMock()
        if model == JiraIntegrationCardsMapping:
            query_mock.filter.return_value.first.return_value = mock_mapping
        else:
            query_mock.filter.return_value.first.return_value = None
        return query_mock

    mock_session.query.side_effect = mock_query

    service.update_issue_column_on_jira(mock_session, story_without_proj, new_col)

    assert mock_session.query.call_count == 2
    mock_session.query.assert_any_call(JiraIntegrationCardsMapping)
    mock_session.query.assert_any_call(Project)


def test_update_issue_column_on_jira_project_not_found(service, mock_session, sample_story):
    new_col = MagicMock(spec=ProjectKanbanColumn, id="col-new")
    mock_mapping = MagicMock(spec=JiraIntegrationCardsMapping)

    def mock_query(model):
        query_mock = MagicMock()
        if model == JiraIntegrationCardsMapping:
            query_mock.filter.return_value.first.return_value = mock_mapping
        else:
            query_mock.filter.return_value.first.return_value = None
        return query_mock

    mock_session.query.side_effect = mock_query

    service.update_issue_column_on_jira(mock_session, sample_story, new_col)

    assert mock_session.query.call_count == 2
    mock_session.query.assert_any_call(JiraIntegrationCardsMapping)
    mock_session.query.assert_any_call(Project)


@patch("services.kanban_service.JiraIntegrationProvider")
def test_update_issue_column_on_jira_no_running_loop(
    mock_jira_provider_class, service, mock_session, sample_story
):
    new_col = MagicMock(spec=ProjectKanbanColumn, id="col-new")
    mock_project = MagicMock(spec=Project, id="project-123")
    mock_session.query.return_value.filter.return_value.first.return_value = mock_project

    mock_provider_instance = MagicMock()
    mock_jira_provider_class.return_value = mock_provider_instance

    with (
        patch("services.kanban_service.asyncio.get_running_loop", side_effect=RuntimeError("no running loop")),
        patch("services.kanban_service.asyncio.run") as mock_asyncio_run,
    ):
        service.update_issue_column_on_jira(mock_session, sample_story, new_col)

        mock_jira_provider_class.assert_called_once_with(mock_session)
        mock_provider_instance.update_issue_column.assert_called_once_with(
            mock_project, sample_story, new_col
        )
        mock_asyncio_run.assert_called_once_with(
            mock_provider_instance.update_issue_column.return_value
        )


@patch("services.kanban_service.nest_asyncio.apply")
@patch("services.kanban_service.JiraIntegrationProvider")
def test_update_issue_column_on_jira_running_loop_is_running(
    mock_jira_provider_class, mock_nest_asyncio_apply, service, mock_session, sample_story
):
    new_col = MagicMock(spec=ProjectKanbanColumn, id="col-new")
    mock_project = MagicMock(spec=Project, id="project-123")
    mock_session.query.return_value.filter.return_value.first.return_value = mock_project

    mock_provider_instance = MagicMock()
    mock_jira_provider_class.return_value = mock_provider_instance

    mock_loop = MagicMock()
    mock_loop.is_running.return_value = True

    with (
        patch("services.kanban_service.asyncio.get_running_loop", return_value=mock_loop),
        patch("services.kanban_service.asyncio.run") as mock_asyncio_run,
    ):
        service.update_issue_column_on_jira(mock_session, sample_story, new_col)

        mock_nest_asyncio_apply.assert_called_once()
        mock_asyncio_run.assert_called_once_with(
            mock_provider_instance.update_issue_column.return_value
        )


@patch("services.kanban_service.JiraIntegrationProvider")
def test_update_issue_column_on_jira_running_loop_is_not_running(
    mock_jira_provider_class, service, mock_session, sample_story
):
    new_col = MagicMock(spec=ProjectKanbanColumn, id="col-new")
    mock_project = MagicMock(spec=Project, id="project-123")
    mock_session.query.return_value.filter.return_value.first.return_value = mock_project

    mock_provider_instance = MagicMock()
    mock_jira_provider_class.return_value = mock_provider_instance

    mock_loop = MagicMock()
    mock_loop.is_running.return_value = False

    with patch("services.kanban_service.asyncio.get_running_loop", return_value=mock_loop):
        service.update_issue_column_on_jira(mock_session, sample_story, new_col)

        mock_loop.run_until_complete.assert_called_once_with(
            mock_provider_instance.update_issue_column.return_value
        )


def test_update_issue_column_on_jira_handles_exception(service, mock_session, sample_story):
    new_col = MagicMock(spec=ProjectKanbanColumn, id="col-new")
    mock_session.query.side_effect = Exception("Database failure")

    # Should not raise exception
    service.update_issue_column_on_jira(mock_session, sample_story, new_col)


# ==============================================================================
# run_kanban_update_flow
# ==============================================================================


@patch("services.kanban_service.notification_service.notify_project_members")
@patch("services.kanban_service.validate_project_kanban_column_in_project")
def test_run_kanban_update_flow_forward_movement(
    mock_validate_col,
    mock_notify,
    service,
    mock_session,
    mock_background_tasks,
    sample_story,
    sample_user,
):
    sample_story.kanban_column.position = 0
    new_col = MagicMock(spec=ProjectKanbanColumn, id="col-1", position=1)
    mock_validate_col.return_value = new_col

    with (
        patch.object(service, "update_kanban_column_and_its_log") as mock_update_log,
        patch.object(service, "update_issue_column_on_jira") as mock_update_jira,
        patch.object(service, "mark_done_if_last_column") as mock_mark_done,
        patch.object(service, "run_kanban_columns_rules") as mock_run_rules,
    ):
        service.run_kanban_update_flow(
            mock_session,
            mock_background_tasks,
            "project-123",
            sample_story,
            "col-1",
            sample_user,
        )

        mock_validate_col.assert_called_once_with(mock_session, "project-123", "col-1")
        mock_update_log.assert_called_once_with(mock_session, sample_story, new_col)
        mock_update_jira.assert_called_once_with(mock_session, sample_story, new_col)
        mock_mark_done.assert_called_once_with(
            mock_session, "project-123", sample_story, mock_background_tasks, sample_user
        )
        mock_notify.assert_called_once_with(
            session=mock_session,
            project_id="project-123",
            message={
                "storyId": sample_story.id,
                "newColumnId": new_col.id,
            },
            exclude_user_id=sample_story.user_id,
            notification_type="KANBAN_UPDATE",
        )
        mock_run_rules.assert_called_once_with(
            mock_session, mock_background_tasks, sample_story, sample_user
        )


@patch("services.kanban_service.notification_service.notify_project_members")
@patch("services.kanban_service.validate_project_kanban_column_in_project")
def test_run_kanban_update_flow_backward_movement_skips_rules(
    mock_validate_col,
    mock_notify,
    service,
    mock_session,
    mock_background_tasks,
    sample_story,
    sample_user,
):
    sample_story.kanban_column.position = 2
    new_col = MagicMock(spec=ProjectKanbanColumn, id="col-0", position=0)
    mock_validate_col.return_value = new_col

    with (
        patch.object(service, "update_kanban_column_and_its_log") as mock_update_log,
        patch.object(service, "update_issue_column_on_jira") as mock_update_jira,
        patch.object(service, "mark_done_if_last_column") as mock_mark_done,
        patch.object(service, "run_kanban_columns_rules") as mock_run_rules,
    ):
        service.run_kanban_update_flow(
            mock_session,
            mock_background_tasks,
            "project-123",
            sample_story,
            "col-0",
            sample_user,
        )

        mock_validate_col.assert_called_once_with(mock_session, "project-123", "col-0")
        mock_update_log.assert_called_once_with(mock_session, sample_story, new_col)
        mock_update_jira.assert_called_once_with(mock_session, sample_story, new_col)
        mock_mark_done.assert_called_once_with(
            mock_session, "project-123", sample_story, mock_background_tasks, sample_user
        )
        mock_notify.assert_called_once_with(
            session=mock_session,
            project_id="project-123",
            message={
                "storyId": sample_story.id,
                "newColumnId": new_col.id,
            },
            exclude_user_id=sample_story.user_id,
            notification_type="KANBAN_UPDATE",
        )
        mock_run_rules.assert_not_called()


# ==============================================================================
# _run_handler_in_background
# ==============================================================================


@patch("services.kanban_service.create_session")
def test_run_handler_in_background_success(mock_create_session, sample_story, sample_user):
    mock_session = MagicMock()
    mock_create_session.return_value.__enter__.return_value = mock_session

    entities = {"user-123": sample_user, "story-123": sample_story}
    mock_session.query.return_value.filter_by.side_effect = lambda **kwargs: MagicMock(
        first=MagicMock(return_value=entities.get(kwargs.get("id")))
    )

    mock_handler = MagicMock()
    _run_handler_in_background(mock_handler, "project-123", "user-123", "story-123")

    mock_handler.assert_called_once_with(
        mock_session, "project-123", sample_user, sample_story
    )


@pytest.mark.parametrize(
    ("user_id", "story_id"),
    [
        ("user-123", "story-missing"),
        ("user-missing", "story-123"),
    ],
)
@patch("services.kanban_service.create_session")
def test_run_handler_in_background_entity_not_found(
    mock_create_session, sample_story, sample_user, user_id, story_id
):
    mock_session = MagicMock()
    mock_create_session.return_value.__enter__.return_value = mock_session

    entities = {"user-123": sample_user, "story-123": sample_story}
    mock_session.query.return_value.filter_by.side_effect = lambda **kwargs: MagicMock(
        first=MagicMock(return_value=entities.get(kwargs.get("id")))
    )

    mock_handler = MagicMock()
    _run_handler_in_background(mock_handler, "project-123", user_id, story_id)

    mock_handler.assert_not_called()
