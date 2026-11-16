"""Resetting the tutor conversation must not break a turn already in flight."""

import uuid

from database.models import TutorConversation, TutorMessage


def _conversation_with_pending_turn(db_session, project, user):
    conversation = TutorConversation(
        id=str(uuid.uuid4()), project_id=project.id, user_id=user.id
    )
    db_session.add(conversation)
    db_session.commit()

    placeholder = TutorMessage(
        id=str(uuid.uuid4()),
        conversation_id=conversation.id,
        role="assistant",
        content="",
        status="processing",
    )
    db_session.add(placeholder)
    db_session.commit()
    return conversation, placeholder


class TestWriteAssistantResult:
    def test_discards_reply_when_placeholder_was_cleared(
        self, db_session, sample_project, sample_user, monkeypatch
    ):
        from agents import tutor_agent

        _, placeholder = _conversation_with_pending_turn(
            db_session, sample_project, sample_user
        )
        placeholder_id = placeholder.id

        db_session.query(TutorMessage).filter(
            TutorMessage.id == placeholder_id
        ).delete()
        db_session.commit()

        monkeypatch.setattr(
            tutor_agent, "create_session", lambda: _NoCloseSession(db_session)
        )

        tutor_agent._write_assistant_result(placeholder_id, "resposta", "completed")

        assert (
            db_session.query(TutorMessage)
            .filter(TutorMessage.id == placeholder_id)
            .one_or_none()
            is None
        )

    def test_writes_reply_when_placeholder_still_exists(
        self, db_session, sample_project, sample_user, monkeypatch
    ):
        from agents import tutor_agent

        _, placeholder = _conversation_with_pending_turn(
            db_session, sample_project, sample_user
        )

        monkeypatch.setattr(
            tutor_agent, "create_session", lambda: _NoCloseSession(db_session)
        )

        tutor_agent._write_assistant_result(placeholder.id, "resposta", "completed")

        db_session.refresh(placeholder)
        assert placeholder.content == "resposta"
        assert placeholder.status == "completed"


class TestResetEndpoint:
    def test_clears_messages_and_summary(
        self, client, project_access_user, sample_project, db_session
    ):
        conversation = TutorConversation(
            id=str(uuid.uuid4()),
            project_id=sample_project.id,
            user_id=project_access_user.id,
            summary="resumo anterior",
        )
        db_session.add(conversation)
        db_session.commit()
        db_session.add(
            TutorMessage(
                id=str(uuid.uuid4()),
                conversation_id=conversation.id,
                role="user",
                content="pergunta",
                status="completed",
            )
        )
        db_session.commit()

        response = client.delete(f"/tutor/{sample_project.id}")

        assert response.status_code == 200
        body = response.json()
        assert body["messages"] == []
        assert body["summary"] is None
        assert db_session.query(TutorMessage).count() == 0


class _NoCloseSession:
    """Hands the test session to code that owns its session via ``with``."""

    def __init__(self, session):
        self._session = session

    def __enter__(self):
        return self._session

    def __exit__(self, *_exc):
        return False
