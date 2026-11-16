"""Router-level behaviour of the tutor endpoints."""

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy.exc import IntegrityError

from agents import tutor_agent
from database.models import TutorConversation, TutorMessage
from src.routers.agents import tutor_router


@pytest.fixture
def no_background_turn():
    with patch.object(tutor_router, "process_tutor_turn") as mock_turn:
        yield mock_turn


def _conversation(db_session, project, user):
    conversation = TutorConversation(
        id=str(uuid.uuid4()), project_id=project.id, user_id=user.id
    )
    db_session.add(conversation)
    db_session.commit()
    return conversation


def _placeholder(db_session, conversation, status="processing"):
    message = TutorMessage(
        id=str(uuid.uuid4()),
        conversation_id=conversation.id,
        role="assistant",
        content="",
        status=status,
    )
    db_session.add(message)
    db_session.commit()
    return message


class TestSendMessage:
    def test_rejects_a_second_turn_while_one_is_running(
        self, client, project_access_user, sample_project, db_session
    ):
        conversation = _conversation(db_session, sample_project, project_access_user)
        _placeholder(db_session, conversation)

        response = client.post(
            f"/tutor/{sample_project.id}", json={"message": "outra pergunta"}
        )

        assert response.status_code == 409

    def test_does_not_persist_the_rejected_message(
        self, client, project_access_user, sample_project, db_session
    ):
        conversation = _conversation(db_session, sample_project, project_access_user)
        _placeholder(db_session, conversation)

        client.post(f"/tutor/{sample_project.id}", json={"message": "outra"})

        assert db_session.query(TutorMessage).filter_by(role="user").count() == 0

    def test_accepts_when_the_previous_turn_finished(
        self, client, project_access_user, sample_project, db_session,
        no_background_turn,
    ):
        conversation = _conversation(db_session, sample_project, project_access_user)
        _placeholder(db_session, conversation, status="completed")

        response = client.post(
            f"/tutor/{sample_project.id}", json={"message": "pode responder"}
        )

        assert response.status_code == 200
        assert response.json()["status"] == "processing"

    def test_persists_user_message_and_placeholder(
        self, client, project_access_user, sample_project, db_session,
        no_background_turn,
    ):
        client.post(f"/tutor/{sample_project.id}", json={"message": "primeira"})

        roles = [m.role for m in db_session.query(TutorMessage).all()]
        assert sorted(roles) == ["assistant", "user"]

    def test_schedules_the_turn_with_both_message_ids(
        self, client, project_access_user, sample_project, db_session,
        no_background_turn,
    ):
        response = client.post(
            f"/tutor/{sample_project.id}", json={"message": "pergunta"}
        )

        placeholder = (
            db_session.query(TutorMessage).filter_by(role="assistant").one()
        )
        user_id = db_session.query(TutorMessage).filter_by(role="user").one().id
        no_background_turn.assert_called_once()
        assert no_background_turn.call_args.args[3:] == (
            user_id,
            placeholder.id,
            placeholder.agent_run_id,
        )
        assert placeholder.agent_run_id is not None
        assert response.json()["id"] == placeholder.id

    def test_empty_message_is_rejected(
        self, client, project_access_user, sample_project
    ):
        response = client.post(f"/tutor/{sample_project.id}", json={"message": ""})

        assert response.status_code == 422


class TestGetConversation:
    def test_creates_the_conversation_on_first_read(
        self, client, project_access_user, sample_project, db_session
    ):
        response = client.get(f"/tutor/{sample_project.id}")

        assert response.status_code == 200
        assert response.json()["messages"] == []
        assert db_session.query(TutorConversation).count() == 1


class TestConversationRace:
    def test_recovers_when_another_request_created_it_first(
        self, db_session, sample_project, sample_user
    ):
        existing = _conversation(db_session, sample_project, sample_user)

        with patch.object(db_session, "add"), patch.object(
            db_session, "rollback"
        ), patch.object(
            db_session, "commit", side_effect=IntegrityError("dup", None, Exception())
        ), patch.object(db_session, "query") as mock_query:
            filtered = mock_query.return_value.filter_by.return_value
            filtered.one_or_none.return_value = None
            filtered.one.return_value = existing

            recovered = tutor_agent.get_or_create_conversation(
                db_session, sample_project.id, sample_user.id
            )

        assert recovered is existing

    def test_rolls_back_before_re_querying(
        self, db_session, sample_project, sample_user
    ):
        existing = _conversation(db_session, sample_project, sample_user)

        with patch.object(db_session, "add"), patch.object(
            db_session, "rollback"
        ) as mock_rollback, patch.object(
            db_session, "commit", side_effect=IntegrityError("dup", None, Exception())
        ), patch.object(db_session, "query") as mock_query:
            filtered = mock_query.return_value.filter_by.return_value
            filtered.one_or_none.return_value = None
            filtered.one.return_value = existing

            tutor_agent.get_or_create_conversation(
                db_session, sample_project.id, sample_user.id
            )

        mock_rollback.assert_called_once()
