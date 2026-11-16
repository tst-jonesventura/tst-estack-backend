"""Expensive per-call work that is safe to reuse for a bounded window."""

import uuid
from unittest.mock import MagicMock, patch

from agents.utils import agent_factory
from agents.utils import mcp_providers as mp
from database.models import Agent, AgentContext, AgentTool, Context, Tool
from services import github_app_service
from utils.ttl_cache import TTLCache


class TestTTLCache:
    def test_returns_what_was_stored(self):
        cache = TTLCache(ttl_seconds=60)
        cache.put("k", "v")

        assert cache.get("k") == "v"

    def test_missing_key_is_none(self):
        assert TTLCache(ttl_seconds=60).get("ausente") is None

    def test_expired_entry_is_dropped(self):
        cache = TTLCache(ttl_seconds=-1)
        cache.put("k", "v")

        assert cache.get("k") is None

    def test_clear_empties_everything(self):
        cache = TTLCache(ttl_seconds=60)
        cache.put("k", "v")
        cache.clear()

        assert cache.get("k") is None


def _company(db_session):
    from tests.test_mcp_providers import _make_company

    return _make_company(db_session)


class TestInstallationTokenCache:
    def test_second_call_does_not_hit_github(self, db_session):
        org = _company(db_session)
        provider = mp.GithubMcpProvider()

        with patch.object(
            github_app_service, "get_installation_access_token", return_value="tok-1"
        ) as mock_mint:
            provider.get_access_token(db_session, org)
            provider.get_access_token(db_session, org)

        mock_mint.assert_called_once()

    def test_both_calls_return_the_same_token(self, db_session):
        org = _company(db_session)
        provider = mp.GithubMcpProvider()

        with patch.object(
            github_app_service, "get_installation_access_token", return_value="tok-1"
        ):
            first, _ = provider.get_access_token(db_session, org)
            second, _ = provider.get_access_token(db_session, org)

        assert first == second == "tok-1"

    def test_expiry_forces_a_new_mint(self, db_session):
        org = _company(db_session)
        provider = mp.GithubMcpProvider()

        with patch.object(
            github_app_service, "get_installation_access_token", return_value="tok-1"
        ) as mock_mint:
            provider.get_access_token(db_session, org)
            mp._installation_token_cache.clear()
            provider.get_access_token(db_session, org)

        assert mock_mint.call_count == 2

    def test_ttl_expires_before_github_does(self):
        assert mp.INSTALLATION_TOKEN_TTL_SECONDS < 60 * 60


def _agent(db_session, name="tutor", model="openai:gpt-5.2"):
    tool = Tool(name="get_stories", description="d")
    context = Context(name="get_all_specs", description="d")
    db_session.add_all([tool, context])
    db_session.commit()

    agent = Agent(
        id=str(uuid.uuid4()),
        name=name,
        model=model,
        is_default=True,
        temperature=None,
        max_retries=3,
    )
    db_session.add(agent)
    db_session.commit()
    db_session.add_all(
        [
            AgentTool(agent_id=agent.id, tool_name="get_stories"),
            AgentContext(agent_id=agent.id, context_name="get_all_specs"),
        ]
    )
    db_session.commit()
    return agent


class TestAgentSpecCache:
    def test_second_lookup_does_not_requery(self, db_session):
        _agent(db_session)

        with patch.object(
            agent_factory, "get_agent_info", wraps=agent_factory.get_agent_info
        ) as spy:
            agent_factory.get_agent_spec(db_session, "tutor", None)
            agent_factory.get_agent_spec(db_session, "tutor", None)

        spy.assert_called_once()

    def test_spec_carries_the_stored_configuration(self, db_session):
        _agent(db_session)

        spec = agent_factory.get_agent_spec(db_session, "tutor", None)

        assert spec.model == "openai:gpt-5.2"
        assert spec.tool_names == ("get_stories",)
        assert spec.context_names == ("get_all_specs",)
        assert spec.temperature is None

    def test_spec_is_immutable(self, db_session):
        _agent(db_session)
        spec = agent_factory.get_agent_spec(db_session, "tutor", None)

        try:
            spec.model = "outro"
        except Exception:
            return
        raise AssertionError("AgentSpec should be frozen")

    def test_missing_agent_is_not_cached(self, db_session):
        assert agent_factory.get_agent_spec(db_session, "inexistente", None) is None

        _agent(db_session, name="inexistente")

        assert agent_factory.get_agent_spec(db_session, "inexistente", None) is not None

    def test_projects_do_not_share_an_entry(self, db_session):
        _agent(db_session)

        agent_factory.get_agent_spec(db_session, "tutor", "projeto-a")

        with patch.object(
            agent_factory, "get_agent_info", wraps=agent_factory.get_agent_info
        ) as spy:
            agent_factory.get_agent_spec(db_session, "tutor", "projeto-b")

        spy.assert_called_once()


class TestMcpFactorySignature:
    def test_git_mcp_uses_the_callers_session(self):
        from agents.utils import mcps as mcps_module

        session = MagicMock()
        with patch.object(
            mcps_module, "resolve_mcp_toolset_for_domain", return_value="toolset"
        ) as mock_resolve:
            mcps_module.MCPS_DICT["git_mcp"]("ask_git", "proj-1", session)

        assert mock_resolve.call_args.args[0] is session

    def test_git_mcp_opens_no_session_of_its_own(self):
        from agents.utils import mcps as mcps_module

        assert not hasattr(mcps_module, "create_session")
