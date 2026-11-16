from contextlib import contextmanager

import pytest

from agents.utils.agent_deps import SOURCE_TOOL_BUDGET
from agents.utils.mcps import MCPS_DICT
from agents.utils.tools import TOOLS_DICT
from database import models
from scripts import seed_ask_database_agent as script
from utils.config import settings


@pytest.fixture
def seed(db_session, monkeypatch):
    @contextmanager
    def _factory():
        yield db_session

    db_session.add(models.Mcp(name="db_mcp", description="registro de teste"))
    db_session.add(models.Tool(name="ask_database", description="registro de teste"))
    db_session.commit()

    monkeypatch.setattr(script, "create_session", _factory)
    monkeypatch.setattr(script, "sync_tools", lambda session: None)
    monkeypatch.setattr(script, "sync_context", lambda session: None)
    monkeypatch.setattr(script, "sync_mcps", lambda session: None)
    return script.seed


def _default(db_session):
    return (
        db_session.query(models.Agent)
        .filter(models.Agent.name == "ask_database", models.Agent.is_default == True)
        .one()
    )


def _tutor(db_session, is_default=True, model="openai:gpt-5.2", prompt="prompt do tutor"):
    tutor = models.Agent(
        name="tutor", is_default=is_default, model=model, system_prompt=prompt, max_retries=3
    )
    db_session.add(tutor)
    db_session.commit()
    return tutor


def _tool_names(db_session, agent):
    return sorted(
        t.tool_name for t in db_session.query(models.AgentTool).filter_by(agent_id=agent.id)
    )


class TestSeed:
    def test_creates_the_default_agent_on_the_default_model(self, db_session, seed):
        seed()

        agent = _default(db_session)
        assert agent.model == settings.default_model
        assert agent.system_prompt == script.ASK_DATABASE_SYSTEM_PROMPT
        assert agent.temperature is None

    def test_wires_the_database_mcp(self, db_session, seed):
        seed()

        mcps = db_session.query(models.AgentMcp).filter_by(agent_id=_default(db_session).id)
        assert [m.mcp_name for m in mcps] == ["db_mcp"]

    def test_keeps_the_model_chosen_for_an_existing_agent(self, db_session, seed):
        db_session.add(
            models.Agent(name="ask_database", is_default=True, model="google-gla:gemini-3.1-pro-preview", max_retries=3)
        )
        db_session.commit()

        seed()

        assert _default(db_session).model == "google-gla:gemini-3.1-pro-preview"

    def test_is_idempotent(self, db_session, seed):
        tutor = _tutor(db_session)

        seed()
        seed()

        assert db_session.query(models.Agent).filter_by(name="ask_database").count() == 1
        assert _tool_names(db_session, tutor) == ["ask_database"]

    def test_gives_every_tutor_the_tool_without_touching_it(self, db_session, seed):
        default = _tutor(db_session)
        override = _tutor(db_session, is_default=False, model="openai:gpt-5.6-luna", prompt="meu")
        db_session.add(models.Tool(name="ask_git", description="x"))
        db_session.add(models.AgentTool(agent_id=default.id, tool_name="ask_git"))
        db_session.commit()

        seed()

        assert _tool_names(db_session, default) == ["ask_database", "ask_git"]
        assert _tool_names(db_session, override) == ["ask_database"]
        db_session.refresh(override)
        assert (override.model, override.system_prompt) == ("openai:gpt-5.6-luna", "meu")

    def test_project_override_on_a_seeded_prompt_is_updated(self, db_session, seed):
        stale = models.Agent(
            name="ask_database",
            is_default=False,
            model="m",
            system_prompt=script.PROMPT_OWNERSHIP_MARKER + " versão antiga",
            max_retries=3,
        )
        custom = models.Agent(
            name="ask_database", is_default=False, model="m", system_prompt="Prompt próprio", max_retries=3
        )
        db_session.add_all([stale, custom])
        db_session.commit()

        seed()

        db_session.refresh(stale)
        db_session.refresh(custom)
        assert stale.system_prompt == script.ASK_DATABASE_SYSTEM_PROMPT
        assert custom.system_prompt == "Prompt próprio"


class TestPrompt:
    SERVER_TOOLS = frozenset({
        "database_overview",
        "list_tables",
        "describe_table",
        "list_relationships",
        "suggest_relationships",
        "list_rules",
        "get_definition",
        "column_profile",
        "sample_rows",
        "run_readonly_query",
    })

    def test_names_tools_the_server_exposes(self):
        for name in self.SERVER_TOOLS:
            assert f"`{name}`" in script.ASK_DATABASE_SYSTEM_PROMPT

    def test_forbids_working_around_the_mask(self):
        assert "[mascarado]" in script.ASK_DATABASE_SYSTEM_PROMPT

    def test_warns_that_links_may_exist_without_foreign_keys(self):
        assert "sem FK" in script.ASK_DATABASE_SYSTEM_PROMPT

    def test_explains_how_to_read_an_as400(self):
        prompt = script.ASK_DATABASE_SYSTEM_PROMPT

        assert "column_text" in prompt
        assert "AAAAMMDD" in prompt
        assert "RPG" in prompt
        assert "hipótese" in prompt


class TestRegistries:
    def test_tool_and_mcp_are_registered(self):
        assert "ask_database" in TOOLS_DICT
        assert "db_mcp" in MCPS_DICT

    def test_descriptions_are_persisted_docstrings(self):
        assert "read-only" in MCPS_DICT["db_mcp"].__doc__.lower()
        assert "relationships" in TOOLS_DICT["ask_database"].__doc__

    def test_data_tools_have_a_run_budget(self):
        assert SOURCE_TOOL_BUDGET["ask_database"] == 3
        assert SOURCE_TOOL_BUDGET["run_readonly_query"] >= SOURCE_TOOL_BUDGET["sample_rows"]
