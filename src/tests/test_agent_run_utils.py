import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic_ai import RunUsage
from sqlalchemy.orm import Session

from agents.utils.agent_run_utils import (
    AgentLogger,
    NullAgentLogger,
    create_agent_logger,
    fail_stale_runs,
)
from database import models


def test_null_agent_logger():
    logger = NullAgentLogger()
    assert bool(logger) is False
    # Verify no-op calls do not raise
    logger.log("test", is_finished=True)
    logger.finish_agent_run()
    logger.finish_agent_run_with_usage("msg", "gpt-4", None)


def test_agent_logger_log_and_finish(db_session: Session, sample_admin_user, sample_project):
    run = models.AgentRun(
        id=str(uuid.uuid4()),
        user_id=sample_admin_user.id,
        project_id=sample_project.id,
        title="Test Run",
        status="running",
    )
    db_session.add(run)
    db_session.commit()

    agent_logger = AgentLogger(agent_run_id=run.id, notify=True)

    with patch("agents.utils.agent_run_utils.create_session") as mock_create_session, \
         patch("agents.utils.agent_run_utils.notification_service.notify_project_members") as mock_notify:
        
        # Make create_session return a context manager giving db_session
        mock_create_session.return_value.__enter__.return_value = db_session
        mock_create_session.return_value.__exit__.return_value = None

        agent_logger.log("Log message 1", is_finished=False)
        assert mock_notify.called

        # Log entry was created
        log_entry = db_session.query(models.AgentLog).filter(models.AgentLog.agent_run_id == run.id).first()
        assert log_entry is not None
        assert log_entry.message == "Log message 1"

        # Log with is_finished=True
        agent_logger.log("Finished log", is_finished=True)
        db_session.refresh(run)
        assert run.status == "finished"


def test_agent_logger_finish_with_usage(sample_admin_user, sample_project):
    usage = RunUsage(
        requests=2,
        tool_calls=3,
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=10,
        cache_write_tokens=5,
        input_audio_tokens=0,
        cache_audio_read_tokens=0,
    )

    agent_run_mock = MagicMock(spec=models.AgentRun)
    mock_session = MagicMock(spec=Session)
    mock_session.query.return_value.filter.return_value.first.return_value = agent_run_mock

    agent_logger = AgentLogger(agent_run_id="run-id-123", notify=True)

    with patch("agents.utils.agent_run_utils.create_session") as mock_create_session, \
         patch("agents.utils.agent_run_utils.notification_service.notify_project_members") as mock_notify:
        
        from contextlib import contextmanager
        @contextmanager
        def session_scope():
            yield mock_session

        mock_create_session.side_effect = session_scope

        agent_logger.finish_agent_run_with_usage("Completed with usage", model="openai:gpt-4o", usage=usage)
        assert mock_notify.called
        assert agent_run_mock.model == "openai:gpt-4o"
        assert agent_run_mock.status == "finished"
        assert agent_run_mock.requests == 2
        assert agent_run_mock.tool_calls == 3
        assert agent_run_mock.input_tokens == 100
        assert agent_run_mock.output_tokens == 50
        assert mock_session.commit.called


def test_agent_logger_run_not_found(db_session: Session):
    agent_logger = AgentLogger(agent_run_id="non-existent-id", notify=True)
    with patch("agents.utils.agent_run_utils.create_session") as mock_create_session:
        from contextlib import contextmanager
        @contextmanager
        def session_scope():
            yield db_session

        mock_create_session.side_effect = session_scope

        # Should not raise even if agent_run does not exist (and AgentLog insert should not fail FK if mock query returns None)
        with patch.object(db_session, "add"), patch.object(db_session, "commit"), patch.object(db_session, "refresh"):
            agent_logger.log("Some message", is_finished=False)
            agent_logger.finish_agent_run()
            agent_logger.finish_agent_run_with_usage("Msg", "gpt-4", None)


def test_create_agent_logger_with_agent_name_resolution(db_session: Session, sample_admin_user, sample_project):
    agent = models.Agent(
        id=str(uuid.uuid4()),
        name="my_test_agent",
        model="gpt-4o",
        is_default=True,
        max_retries=1,
    )
    db_session.add(agent)
    db_session.commit()

    with patch("agents.utils.agent_run_utils.notification_service.notify_project_members") as mock_notify:
        logger = create_agent_logger(
            session=db_session,
            user_id=sample_admin_user.id,
            project_id=sample_project.id,
            agent_run_title="Resolved agent run",
            agent_name="my_test_agent",
        )
        assert isinstance(logger, AgentLogger)
        assert mock_notify.called

        run = db_session.query(models.AgentRun).filter(models.AgentRun.id == logger.agent_run_id).first()
        assert run is not None
        assert run.agent_id == agent.id
        assert run.status == "running"


def test_create_agent_logger_direct_agent_id(db_session: Session, sample_admin_user, sample_project):
    agent = models.Agent(
        id=str(uuid.uuid4()),
        name="direct_agent",
        model="gpt-4o",
        is_default=True,
        max_retries=1,
    )
    db_session.add(agent)
    db_session.commit()

    with patch("agents.utils.agent_run_utils.notification_service.notify_project_members") as mock_notify:
        logger = create_agent_logger(
            session=db_session,
            user_id=sample_admin_user.id,
            project_id=sample_project.id,
            agent_run_title="Direct agent id run",
            agent_id=agent.id,
        )
        assert isinstance(logger, AgentLogger)
        assert mock_notify.called

        run = db_session.query(models.AgentRun).filter(models.AgentRun.id == logger.agent_run_id).first()
        assert run is not None
        assert run.agent_id == agent.id


def test_fail_stale_runs_no_stale(db_session: Session, sample_project):
    count = fail_stale_runs(db_session, project_id=sample_project.id)
    assert count == 0


def test_fail_stale_runs_with_stale_runs(db_session: Session, sample_admin_user, sample_project):
    from datetime import datetime, timezone
    from schemas.base import date_now
    from agents.utils.agent_run_utils import STALE_RUN_TIMEOUT

    # Stale run created way in the past without logs
    stale_created_at = date_now() - STALE_RUN_TIMEOUT - STALE_RUN_TIMEOUT
    stale_run = models.AgentRun(
        id=str(uuid.uuid4()),
        user_id=sample_admin_user.id,
        project_id=sample_project.id,
        title="Stale Run",
        status="running",
        created_at=stale_created_at,
    )
    db_session.add(stale_run)
    db_session.commit()

    count = fail_stale_runs(db_session, project_id=sample_project.id)
    assert count == 1
    db_session.refresh(stale_run)
    assert stale_run.status == "failed"
