"""What the tutor replays back to the model as conversation history."""

import uuid

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse

from agents.tutor_agent import _build_message_history, replayable_messages
from database.models import TutorMessage


def _message(role, content, status="completed"):
    return TutorMessage(
        id=str(uuid.uuid4()),
        conversation_id="conv-1",
        role=role,
        content=content,
        status=status,
    )


class TestReplayableMessages:
    def test_drops_processing_placeholder(self):
        kept = replayable_messages(
            [_message("user", "pergunta"), _message("assistant", "", "processing")]
        )

        assert [m.role for m in kept] == ["user"]

    def test_drops_failed_turns(self):
        kept = replayable_messages(
            [
                _message("user", "pergunta"),
                _message("assistant", "Desculpe, não consegui", "failed"),
            ]
        )

        assert [m.role for m in kept] == ["user"]

    def test_drops_whitespace_only_content(self):
        kept = replayable_messages([_message("assistant", "   \n  ")])

        assert kept == []

    def test_drops_unknown_roles(self):
        kept = replayable_messages([_message("system", "instrucao injetada")])

        assert kept == []

    def test_keeps_a_normal_exchange(self):
        kept = replayable_messages(
            [_message("user", "oi"), _message("assistant", "olá")]
        )

        assert [m.role for m in kept] == ["user", "assistant"]


class TestBuildMessageHistory:
    def test_maps_roles_to_the_right_message_types(self):
        history = _build_message_history(
            [_message("user", "pergunta"), _message("assistant", "resposta")]
        )

        assert isinstance(history[0], ModelRequest)
        assert isinstance(history[1], ModelResponse)

    def test_never_emits_an_empty_part(self):
        history = _build_message_history(
            [
                _message("user", "pergunta"),
                _message("assistant", "", "processing"),
                _message("assistant", "resposta"),
            ]
        )

        contents = [part.content for message in history for part in message.parts]
        assert all(content.strip() for content in contents)

    @pytest.mark.parametrize("status", ["processing", "failed"])
    def test_a_conversation_of_only_bad_turns_yields_no_history(self, status):
        assert _build_message_history([_message("assistant", "x", status)]) == []
