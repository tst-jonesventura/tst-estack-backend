"""A tutor turn must not be reported as failed while it is still producing."""

import asyncio
import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

from agents import tutor_agent
from database.models import AgentLog, AgentRun, TutorConversation, TutorMessage
from schemas.base import date_now


def _conversation(db_session, project, user):
    conversation = TutorConversation(
        id=str(uuid.uuid4()), project_id=project.id, user_id=user.id
    )
    db_session.add(conversation)
    db_session.commit()
    return conversation


def _run(db_session, project, user):
    run = AgentRun(
        id=str(uuid.uuid4()),
        user_id=user.id,
        project_id=project.id,
        title="Tutor",
        status="running",
        is_internal=True,
    )
    db_session.add(run)
    db_session.commit()
    return run


def _log(db_session, run, age: timedelta):
    entry = AgentLog(
        id=str(uuid.uuid4()),
        agent_run_id=run.id,
        message="Chamando ferramenta 'search_code'",
        timestamp=date_now() - age,
    )
    db_session.add(entry)
    db_session.commit()
    return entry


def _placeholder(db_session, conversation, age: timedelta, run=None):
    message = TutorMessage(
        id=str(uuid.uuid4()),
        conversation_id=conversation.id,
        role="assistant",
        content="",
        status="processing",
        created_at=date_now() - age,
        agent_run_id=run.id if run else None,
    )
    db_session.add(message)
    db_session.commit()
    return message


class TestStaleTimeout:
    def test_working_turn_survives_far_past_the_timeout(
        self, db_session, sample_project, sample_user
    ):
        conversation = _conversation(db_session, sample_project, sample_user)
        run = _run(db_session, sample_project, sample_user)
        message = _placeholder(
            db_session, conversation, timedelta(minutes=30), run=run
        )
        _log(db_session, run, timedelta(seconds=30))

        assert (
            tutor_agent.fail_stale_processing_messages(db_session, conversation.id)
            is False
        )

        db_session.refresh(message)
        assert message.status == "processing"

    def test_silent_turn_is_reconciled(
        self, db_session, sample_project, sample_user
    ):
        conversation = _conversation(db_session, sample_project, sample_user)
        run = _run(db_session, sample_project, sample_user)
        message = _placeholder(
            db_session, conversation, timedelta(minutes=30), run=run
        )
        _log(db_session, run, tutor_agent.STALE_TURN_TIMEOUT + timedelta(minutes=1))

        assert (
            tutor_agent.fail_stale_processing_messages(db_session, conversation.id)
            is True
        )

        db_session.refresh(message)
        assert message.status == "failed"

    def test_turn_without_logs_falls_back_to_creation_time(
        self, db_session, sample_project, sample_user
    ):
        conversation = _conversation(db_session, sample_project, sample_user)
        run = _run(db_session, sample_project, sample_user)
        message = _placeholder(
            db_session,
            conversation,
            tutor_agent.STALE_TURN_TIMEOUT + timedelta(minutes=1),
            run=run,
        )

        tutor_agent.fail_stale_processing_messages(db_session, conversation.id)

        db_session.refresh(message)
        assert message.status == "failed"

    def test_fresh_turn_without_logs_is_left_alone(
        self, db_session, sample_project, sample_user
    ):
        conversation = _conversation(db_session, sample_project, sample_user)
        run = _run(db_session, sample_project, sample_user)
        message = _placeholder(db_session, conversation, timedelta(seconds=5), run=run)

        tutor_agent.fail_stale_processing_messages(db_session, conversation.id)

        db_session.refresh(message)
        assert message.status == "processing"

    def test_reconciliation_closes_the_orphaned_run(
        self, db_session, sample_project, sample_user
    ):
        conversation = _conversation(db_session, sample_project, sample_user)
        run = _run(db_session, sample_project, sample_user)
        _placeholder(
            db_session,
            conversation,
            tutor_agent.STALE_TURN_TIMEOUT + timedelta(minutes=1),
            run=run,
        )

        tutor_agent.fail_stale_processing_messages(db_session, conversation.id)

        db_session.refresh(run)
        assert run.status == "failed"

    def test_timeout_is_short_now_that_it_measures_activity(self):
        assert tutor_agent.STALE_TURN_TIMEOUT <= timedelta(minutes=5)


class TestLastActivityAt:
    def test_uses_the_newest_log(self, db_session, sample_project, sample_user):
        conversation = _conversation(db_session, sample_project, sample_user)
        run = _run(db_session, sample_project, sample_user)
        message = _placeholder(
            db_session, conversation, timedelta(minutes=10), run=run
        )
        _log(db_session, run, timedelta(minutes=9))
        _log(db_session, run, timedelta(minutes=1))

        activity = tutor_agent.last_activity_at(db_session, message)

        assert activity > date_now() - timedelta(minutes=2)

    def test_unlinked_message_uses_its_own_creation_time(
        self, db_session, sample_project, sample_user
    ):
        conversation = _conversation(db_session, sample_project, sample_user)
        message = _placeholder(db_session, conversation, timedelta(minutes=7))

        activity = tutor_agent.last_activity_at(db_session, message)

        assert activity == tutor_agent._as_utc(message.created_at)

    def test_always_returns_an_aware_datetime(
        self, db_session, sample_project, sample_user
    ):
        conversation = _conversation(db_session, sample_project, sample_user)
        run = _run(db_session, sample_project, sample_user)
        message = _placeholder(db_session, conversation, timedelta(minutes=2), run=run)
        _log(db_session, run, timedelta(minutes=1))

        assert tutor_agent.last_activity_at(db_session, message).tzinfo is not None


class TestCancellation:
    def test_cancelled_turn_marks_the_placeholder_failed(self):
        writes, runs = [], []

        with patch.object(tutor_agent, "create_session"), patch.object(
            tutor_agent, "get_or_create_conversation"
        ), patch.object(
            tutor_agent, "AgentLogger", MagicMock()
        ), patch.object(
            tutor_agent,
            "create_agent_from_database",
            side_effect=asyncio.CancelledError(),
        ), patch.object(
            tutor_agent, "_write_assistant_result", side_effect=lambda *a: writes.append(a)
        ), patch.object(
            tutor_agent, "_finish_run", side_effect=lambda *a, **k: runs.append((a, k))
        ):
            with pytest.raises(asyncio.CancelledError):
                tutor_agent.process_tutor_turn(
                    "p", "u", "msg", "user-1", "assist-1", "run-1"
                )

        assert writes == [("assist-1", tutor_agent.FAILURE_MESSAGE, "failed")]
        assert runs
        assert runs[0][1].get("status") == "failed"
