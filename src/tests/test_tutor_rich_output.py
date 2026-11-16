"""The tutor's answers keep their symbols and render as tables and diagrams.

tutor_message.content was VARCHAR with a Latin1 collation, so every emoji in a
permission matrix was stored as "?" and tree-drawing characters became "+--". The
frontend now renders GFM tables and ```mermaid blocks, and the tutor is told how.
"""

import importlib.util
import pathlib
import uuid
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mssql

from agents import tutor_agent
from database.models import TutorConversation, TutorMessage

RICH_TEXT = "✅ ❌ ⚠️ ├── └── ação 🚀"

MIGRATION = next(
    pathlib.Path(__file__)
    .resolve()
    .parents[2]
    .joinpath("alembic", "versions")
    .glob("*-tutor_text_as_nvarchar.py")
)


def _migration_module():
    spec = importlib.util.spec_from_file_location("tutor_nvarchar", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestColumnsKeepEverySymbol:
    @pytest.mark.parametrize(
        "column",
        [TutorMessage.__table__.c.content, TutorConversation.__table__.c.summary],
    )
    def test_the_column_is_nvarchar_on_sql_server(self, column):
        assert column.type.compile(dialect=mssql.dialect()) == "NVARCHAR(max)"

    def test_the_type_is_not_the_deprecated_ntext(self):
        compiled = TutorMessage.__table__.c.content.type.compile(dialect=mssql.dialect())
        assert "NTEXT" not in compiled

    def test_a_message_round_trips_its_symbols(
        self, db_session, sample_project, sample_user
    ):
        conversation = TutorConversation(
            id=str(uuid.uuid4()), project_id=sample_project.id, user_id=sample_user.id
        )
        db_session.add(conversation)
        db_session.flush()
        message = TutorMessage(
            id=str(uuid.uuid4()),
            conversation_id=conversation.id,
            role="assistant",
            content=RICH_TEXT,
            status="completed",
        )
        db_session.add(message)
        db_session.commit()
        db_session.expire_all()

        assert db_session.get(TutorMessage, message.id).content == RICH_TEXT


class TestMigration:
    def test_it_follows_the_previous_head(self):
        assert _migration_module().down_revision == "4eb3156a9a04"

    @pytest.mark.parametrize(
        "table,column,nullable",
        [("tutor_message", "content", False), ("tutor_conversation", "summary", True)],
    )
    def test_it_converts_each_column_keeping_its_nullability(
        self, table, column, nullable
    ):
        module = _migration_module()
        with patch.object(module, "op") as op:
            module.upgrade()

        call = next(
            c for c in op.alter_column.call_args_list if c.args == (table, column)
        )
        assert isinstance(call.kwargs["type_"], sa.Unicode)
        assert call.kwargs["nullable"] is nullable
        assert call.kwargs["existing_nullable"] is nullable

    def test_downgrade_restores_varchar(self):
        module = _migration_module()
        with patch.object(module, "op") as op:
            module.downgrade()

        types = {type(c.kwargs["type_"]) for c in op.alter_column.call_args_list}
        assert types == {sa.String}


class TestTutorIsToldHowAnswersRender:
    def test_diagrams_are_requested_as_mermaid_blocks(self):
        assert "```mermaid" in tutor_agent.TUTOR_OUTPUT_INSTRUCTIONS

    @pytest.mark.parametrize(
        "diagram",
        ["flowchart LR", "sequenceDiagram", "classDiagram", "erDiagram", "stateDiagram-v2"],
    )
    def test_each_diagram_kind_is_mapped_to_its_use(self, diagram):
        assert diagram in tutor_agent.TUTOR_OUTPUT_INSTRUCTIONS

    def test_node_ids_must_avoid_spaces_and_accents(self):
        assert "sem espaços nem acentos" in tutor_agent.TUTOR_OUTPUT_INSTRUCTIONS

    def test_tables_must_be_real_markdown_tables(self):
        assert "| --- |" in tutor_agent.TUTOR_OUTPUT_INSTRUCTIONS

    def test_html_is_ruled_out_because_it_is_not_rendered(self):
        assert "HTML não é renderizado" in tutor_agent.TUTOR_OUTPUT_INSTRUCTIONS

    def test_diagrams_must_not_invent_components(self):
        assert "Não invente componentes" in tutor_agent.TUTOR_OUTPUT_INSTRUCTIONS

    def test_every_turn_sends_the_instructions(self):
        captured = {}
        agent = MagicMock()
        agent.model = "openai:gpt-5.2"

        def run_sync(*args, **kwargs):
            captured.update(kwargs)
            return MagicMock(output="resposta", usage=MagicMock())

        agent.run_sync = run_sync
        conversation = MagicMock(messages=[], summary=None)

        with patch.object(tutor_agent, "create_session"), patch.object(
            tutor_agent, "get_or_create_conversation", return_value=conversation
        ), patch.object(tutor_agent, "AgentLogger", MagicMock()), patch.object(
            tutor_agent, "create_agent_from_database", return_value=agent
        ), patch.object(tutor_agent, "_write_assistant_result"), patch.object(
            tutor_agent, "_finish_run"
        ), patch.object(tutor_agent, "_update_rolling_summary"), patch.object(
            tutor_agent, "tutor_sources_instructions", return_value="## Fontes do projeto"
        ):
            tutor_agent.process_tutor_turn("p", "u", "msg", "user-1", "assist-1", "run-1")

        assert captured["instructions"] == (
            f"{tutor_agent.TUTOR_OUTPUT_INSTRUCTIONS}\n\n## Fontes do projeto"
        )
