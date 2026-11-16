"""The ask_git prompt is versioned here so it can be reviewed and corrected.

It previously existed only as a database row holding a single space, which is how
the agent came to answer a question about the "Tutor" feature by describing the
agent-run machinery that merely contains the string "Tutor".
"""

from contextlib import contextmanager

import pytest

from database import models
from scripts import seed_ask_git_agent as script


@pytest.fixture
def seed(db_session, monkeypatch):
    """Runs the seed against the test session, with the registries pre-populated.

    ``AgentMcp.mcp_name`` is a foreign key into the ``mcp`` registry, which the
    real script fills through ``sync_mcps``.
    """

    @contextmanager
    def _factory():
        yield db_session

    for mcp_name in script.ASK_GIT_MCPS:
        db_session.add(models.Mcp(name=mcp_name, description="registro de teste"))
    db_session.commit()

    monkeypatch.setattr(script, "create_session", _factory)
    monkeypatch.setattr(script, "sync_tools", lambda session: None)
    monkeypatch.setattr(script, "sync_context", lambda session: None)
    monkeypatch.setattr(script, "sync_mcps", lambda session: None)
    return script.seed


def _agent(db_session):
    return (
        db_session.query(models.Agent)
        .filter(models.Agent.name == "ask_git", models.Agent.is_default == True)
        .one()
    )


class TestSeed:
    def test_creates_the_default_agent(self, db_session, seed):
        seed()

        assert _agent(db_session).model == script.ASK_GIT_MODEL

    def test_wires_the_source_code_mcp(self, db_session, seed):
        seed()

        mcps = db_session.query(models.AgentMcp).filter_by(
            agent_id=_agent(db_session).id
        ).all()
        assert [m.mcp_name for m in mcps] == ["git_mcp"]

    def test_replaces_the_placeholder_prompt(self, db_session, seed):
        db_session.add(
            models.Agent(
                name="ask_git",
                is_default=True,
                model="openai:gpt-4.1-nano",
                system_prompt=" ",
                user_prompt="Use the mcp tools",
                temperature=0.1,
                max_retries=3,
            )
        )
        db_session.commit()

        seed()

        agent = _agent(db_session)
        assert agent.system_prompt == script.ASK_GIT_SYSTEM_PROMPT
        assert agent.user_prompt is None

    def test_leaves_no_temperature_pinned(self, db_session, seed):
        seed()

        assert _agent(db_session).temperature is None

    def test_is_idempotent(self, db_session, seed):
        seed()
        seed()

        agents = (
            db_session.query(models.Agent)
            .filter(models.Agent.name == "ask_git")
            .all()
        )
        mcps = db_session.query(models.AgentMcp).filter_by(
            agent_id=_agent(db_session).id
        ).all()
        assert len(agents) == 1
        assert len(mcps) == 1


class TestPromptGuardsAgainstNameCollision:
    def test_states_that_a_name_match_is_not_proof(self):
        assert "NÃO prova" in script.ASK_GIT_SYSTEM_PROMPT

    def test_requires_opening_the_file_before_asserting(self):
        assert "abra o arquivo e confirme" in script.ASK_GIT_SYSTEM_PROMPT

    def test_requires_reporting_ambiguity_instead_of_choosing(self):
        assert "relate a ambiguidade" in script.ASK_GIT_SYSTEM_PROMPT

    def test_names_only_tools_the_server_actually_exposes(self):
        """get_repository_tree was never once callable; pointing at it wasted reads."""
        assert "get_repository_tree" not in script.ASK_GIT_SYSTEM_PROMPT
        assert "get_file_contents" in script.ASK_GIT_SYSTEM_PROMPT

    def test_tells_the_model_to_search_by_entity(self):
        assert "TutorMessage" in script.ASK_GIT_SYSTEM_PROMPT

    def test_points_state_questions_at_model_and_schema_files(self):
        assert "modelo, schema ou tipos" in script.ASK_GIT_SYSTEM_PROMPT

    def test_says_an_empty_search_is_not_proof_of_absence(self):
        assert "não prova ausência" in script.ASK_GIT_SYSTEM_PROMPT

    def test_asks_to_separate_verified_from_assumed(self):
        assert "suposição" in script.ASK_GIT_SYSTEM_PROMPT


class TestProjectOverrides:
    """Agents resolve to a project override first, so the default is not enough.

    The QA project used an override still holding the placeholder prompt, which
    would have made the whole fix invisible to the very scenario it targets.
    """

    def _override(self, db_session, *, prompt=" ", model="openai:gpt-5.6-luna"):
        agent = models.Agent(
            name="ask_git",
            is_default=False,
            model=model,
            system_prompt=prompt,
            user_prompt="Use the mcp tools",
            temperature=0.1,
            max_retries=3,
        )
        db_session.add(agent)
        db_session.commit()
        db_session.refresh(agent)
        return agent

    def test_placeholder_override_receives_the_versioned_prompt(
        self, db_session, seed
    ):
        override = self._override(db_session)

        seed()

        db_session.refresh(override)
        assert override.system_prompt == script.ASK_GIT_SYSTEM_PROMPT

    def test_the_stale_user_prompt_is_cleared(self, db_session, seed):
        override = self._override(db_session)

        seed()

        db_session.refresh(override)
        assert override.user_prompt is None

    def test_the_pinned_temperature_is_released(self, db_session, seed):
        override = self._override(db_session)

        seed()

        db_session.refresh(override)
        assert override.temperature is None

    def test_the_projects_model_choice_is_preserved(self, db_session, seed):
        override = self._override(db_session, model="google:gemini-2.5-flash-lite")

        seed()

        db_session.refresh(override)
        assert override.model == "google:gemini-2.5-flash-lite"

    def test_a_project_that_wrote_its_own_prompt_keeps_it(self, db_session, seed):
        override = self._override(db_session, prompt="Instrucoes proprias do time.")

        seed()

        db_session.refresh(override)
        assert override.system_prompt == "Instrucoes proprias do time."

    def test_an_empty_prompt_counts_as_unset(self, db_session, seed):
        override = self._override(db_session, prompt="")

        seed()

        db_session.refresh(override)
        assert override.system_prompt == script.ASK_GIT_SYSTEM_PROMPT

    def test_reports_how_many_overrides_were_adopted(self, db_session, seed):
        self._override(db_session)
        self._override(db_session)
        self._override(db_session, prompt="Instrucoes proprias.")

        adopted = script.adopt_project_overrides(db_session)

        assert len(adopted) == 2


class TestSeededPromptsGetCorrections:
    """A correction to this prompt must reach overrides that already hold an
    earlier version of it, not just the ones that are still blank."""

    OLD_VERSION = (
        "Você responde perguntas sobre o código-fonte de um projeto lendo os "
        "repositórios dele.\n\n1. Comece pela estrutura: use `get_repository_tree`."
    )

    def test_an_earlier_version_of_our_prompt_is_recognised(self):
        assert script.is_seeded_prompt(self.OLD_VERSION)

    def test_a_blank_prompt_is_recognised(self):
        assert script.is_seeded_prompt(" ")
        assert script.is_seeded_prompt("")
        assert script.is_seeded_prompt(None)

    def test_a_prompt_written_by_the_team_is_not_touched(self):
        assert not script.is_seeded_prompt("Instrucoes proprias do time.")

    def test_the_marker_matches_the_current_prompt(self):
        assert script.PROMPT_OWNERSHIP_MARKER in script.ASK_GIT_SYSTEM_PROMPT

    def test_an_override_on_the_old_version_is_upgraded(self, db_session, seed):
        override = models.Agent(
            name="ask_git",
            is_default=False,
            model="openai:gpt-5.6-luna",
            system_prompt=self.OLD_VERSION,
            max_retries=3,
        )
        db_session.add(override)
        db_session.commit()

        seed()

        db_session.refresh(override)
        assert override.system_prompt == script.ASK_GIT_SYSTEM_PROMPT
        assert "get_repository_tree" not in override.system_prompt
