"""The rolling summary folds forward instead of re-reading the whole backlog."""

import uuid
from unittest.mock import patch

from agents import tutor_agent
from database.models import TutorConversation, TutorMessage


def _conversation(db_session, project, user, summary=None, summarized=0):
    conversation = TutorConversation(
        id=str(uuid.uuid4()),
        project_id=project.id,
        user_id=user.id,
        summary=summary,
        summarized_message_count=summarized,
    )
    db_session.add(conversation)
    db_session.commit()
    return conversation


def _messages(db_session, conversation, count):
    for i in range(count):
        db_session.add(
            TutorMessage(
                id=str(uuid.uuid4()),
                conversation_id=conversation.id,
                role="user" if i % 2 == 0 else "assistant",
                content=f"mensagem {i}",
                status="completed",
            )
        )
    db_session.commit()


class _SessionShim:
    def __init__(self, session):
        self._session = session

    def __enter__(self):
        return self._session

    def __exit__(self, *exc):
        return False


def _run_update(db_session, project, user, summary_result="resumo novo"):
    captured = {}

    def fake_summarize(messages, previous_summary=None):
        captured["messages"] = list(messages)
        captured["previous"] = previous_summary
        return summary_result

    with patch.object(
        tutor_agent, "create_session", return_value=_SessionShim(db_session)
    ), patch.object(tutor_agent, "_summarize_older_messages", fake_summarize):
        tutor_agent._update_rolling_summary(project.id, user.id)

    return captured


class TestIncrementalSummary:
    def test_does_nothing_below_the_window(
        self, db_session, sample_project, sample_user
    ):
        conversation = _conversation(db_session, sample_project, sample_user)
        _messages(db_session, conversation, tutor_agent.MAX_HISTORY_MESSAGES)

        captured = _run_update(db_session, sample_project, sample_user)

        assert captured == {}
        db_session.refresh(conversation)
        assert conversation.summary is None

    def test_folds_only_the_newly_evicted_messages(
        self, db_session, sample_project, sample_user
    ):
        conversation = _conversation(
            db_session, sample_project, sample_user, summary="resumo antigo", summarized=2
        )
        _messages(db_session, conversation, tutor_agent.MAX_HISTORY_MESSAGES + 5)

        captured = _run_update(db_session, sample_project, sample_user)

        assert [m.content for m in captured["messages"]] == [
            "mensagem 2",
            "mensagem 3",
            "mensagem 4",
        ]

    def test_passes_the_previous_summary_forward(
        self, db_session, sample_project, sample_user
    ):
        conversation = _conversation(
            db_session, sample_project, sample_user, summary="resumo antigo", summarized=2
        )
        _messages(db_session, conversation, tutor_agent.MAX_HISTORY_MESSAGES + 5)

        captured = _run_update(db_session, sample_project, sample_user)

        assert captured["previous"] == "resumo antigo"

    def test_advances_the_watermark(
        self, db_session, sample_project, sample_user
    ):
        conversation = _conversation(db_session, sample_project, sample_user)
        _messages(db_session, conversation, tutor_agent.MAX_HISTORY_MESSAGES + 3)

        _run_update(db_session, sample_project, sample_user)

        db_session.refresh(conversation)
        assert conversation.summarized_message_count == 3
        assert conversation.summary == "resumo novo"

    def test_second_run_with_no_new_evictions_is_a_noop(
        self, db_session, sample_project, sample_user
    ):
        conversation = _conversation(db_session, sample_project, sample_user)
        _messages(db_session, conversation, tutor_agent.MAX_HISTORY_MESSAGES + 3)

        _run_update(db_session, sample_project, sample_user)
        captured = _run_update(db_session, sample_project, sample_user)

        assert captured == {}

    def test_failed_messages_do_not_count_towards_the_window(
        self, db_session, sample_project, sample_user
    ):
        conversation = _conversation(db_session, sample_project, sample_user)
        _messages(db_session, conversation, tutor_agent.MAX_HISTORY_MESSAGES)
        db_session.add(
            TutorMessage(
                id=str(uuid.uuid4()),
                conversation_id=conversation.id,
                role="assistant",
                content="falhou",
                status="failed",
            )
        )
        db_session.commit()

        captured = _run_update(db_session, sample_project, sample_user)

        assert captured == {}


class TestSummaryPrompt:
    def test_previous_summary_is_wrapped_for_the_model(self):
        sent = {}

        class FakeAgent:
            def __init__(self, **kwargs):
                pass

            def run_sync(self, prompt):
                sent["prompt"] = prompt
                return type("R", (), {"output": "ok"})()

        message = TutorMessage(
            id="m1", conversation_id="c", role="user", content="oi", status="completed"
        )
        with patch.object(tutor_agent, "Agent", FakeAgent):
            tutor_agent._summarize_older_messages([message], "resumo anterior")

        assert "<resumo_anterior>" in sent["prompt"]
        assert "resumo anterior" in sent["prompt"]
        assert "<novas_mensagens>" in sent["prompt"]

    def test_without_previous_summary_the_prompt_is_just_the_transcript(self):
        sent = {}

        class FakeAgent:
            def __init__(self, **kwargs):
                pass

            def run_sync(self, prompt):
                sent["prompt"] = prompt
                return type("R", (), {"output": "ok"})()

        message = TutorMessage(
            id="m1", conversation_id="c", role="user", content="oi", status="completed"
        )
        with patch.object(tutor_agent, "Agent", FakeAgent):
            tutor_agent._summarize_older_messages([message], None)

        assert "<resumo_anterior>" not in sent["prompt"]
