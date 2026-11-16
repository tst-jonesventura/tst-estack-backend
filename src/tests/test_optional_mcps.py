"""A repository that cannot be reached must not take down a whole tutor turn.

A tutor override had git_mcp attached directly. With the encryption key missing,
building that toolset raised during agent creation, outside every handler, and the
user got only a generic failure. The tutor should answer from specs instead.
"""

from unittest.mock import MagicMock, patch

import pytest

from agents.utils import agent_factory as factory
from agents.utils import mcp_providers as mp
from tests.test_mcp_providers import _make_company
from utils.crypto import DecryptionError, EncryptionKeyMissingError


def _raise(error):
    def factory_fn(agent_name, project_id, session):
        raise error

    return factory_fn


class TestDecryptionFailuresAreConfigurationFailures:
    @pytest.mark.parametrize(
        "error",
        [EncryptionKeyMissingError("sem chave"), DecryptionError("chave errada")],
    )
    def test_loading_the_config_wraps_the_crypto_error(self, db_session, error):
        org = _make_company(db_session)
        session = MagicMock()
        session.query.return_value.filter.return_value.one_or_none.side_effect = error
        provider = mp.GithubMcpProvider()

        with pytest.raises(mp.McpConnectionError) as raised:
            provider._load_config(session, org)

        assert raised.value.__cause__ is error


class TestOptionalMcps:
    def test_an_unavailable_mcp_is_skipped_when_optional(self):
        logger = MagicMock()
        with patch.dict(
            factory.MCPS_DICT, {"git_mcp": _raise(mp.McpConnectionError("x"))}
        ):
            toolsets = factory.build_mcp_toolsets(
                "tutor", "p", MagicMock(), ("git_mcp",), logger, optional=True
            )

        assert toolsets == []
        logger.log.assert_called_once_with(factory.MCP_UNAVAILABLE_LOG)

    @patch.dict(factory.MCPS_DICT, {"git_mcp": _raise(mp.McpConnectionError("x"))})
    def test_an_unavailable_mcp_still_fails_when_required(self):
        session, logger = MagicMock(), MagicMock()

        with pytest.raises(mp.McpConnectionError):
            factory.build_mcp_toolsets(
                "code_git", "p", session, ("git_mcp",), logger, optional=False
            )

    def test_available_mcps_are_kept_alongside_a_skipped_one(self):
        working = MagicMock(return_value="toolset-ok")
        with patch.dict(
            factory.MCPS_DICT,
            {"git_mcp": _raise(mp.McpConnectionError("x")), "ask_devin": working},
        ):
            toolsets = factory.build_mcp_toolsets(
                "tutor", "p", MagicMock(), ("git_mcp", "ask_devin"), None, optional=True
            )

        assert toolsets == ["toolset-ok"]

    @patch.dict(factory.MCPS_DICT, {"git_mcp": _raise(RuntimeError("bug real"))})
    def test_other_errors_are_not_swallowed(self):
        session = MagicMock()

        with pytest.raises(RuntimeError):
            factory.build_mcp_toolsets(
                "tutor", "p", session, ("git_mcp",), None, optional=True
            )

    def test_the_tutor_asks_for_optional_mcps(self):
        from agents import tutor_agent

        with patch.object(tutor_agent, "create_session"), patch.object(
            tutor_agent, "get_or_create_conversation",
            return_value=MagicMock(messages=[], summary=None),
        ), patch.object(tutor_agent, "AgentLogger", MagicMock()), patch.object(
            tutor_agent, "create_agent_from_database"
        ) as create, patch.object(tutor_agent, "_write_assistant_result"), patch.object(
            tutor_agent, "_finish_run"
        ), patch.object(tutor_agent, "_update_rolling_summary"):
            create.return_value.run_sync.return_value = MagicMock(output="ok")
            tutor_agent.process_tutor_turn("p", "u", "msg", "user-1", "assist-1", "run-1")

        assert create.call_args.kwargs["optional_mcps"] is True
