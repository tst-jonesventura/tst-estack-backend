"""Agent runs start with the shared tool budget, and MCP calls have deadlines."""

from unittest.mock import MagicMock, patch

from agents.utils import mcps
from agents.utils.agent_deps import SOURCE_TOOL_BUDGET
from agents.utils.mcp_providers import (
    MCP_INIT_TIMEOUT_SECONDS,
    MCP_READ_TIMEOUT_SECONDS,
)
from services.agent_service import AgentService


def _deps():
    return AgentService()._create_agent_deps(
        "project", "user", "story", MagicMock(), MagicMock()
    )


class TestDepsFactory:
    def test_runs_start_with_the_shared_budget(self):
        assert _deps().custom_usage_tool_limit == SOURCE_TOOL_BUDGET

    def test_the_budget_is_not_shared_between_runs(self):
        first = _deps()
        first.custom_usage_tool_limit["ask_git"] = 1

        assert _deps().custom_usage_tool_limit == SOURCE_TOOL_BUDGET

    def test_the_factory_wires_the_user_story_input(self):
        deps = _deps()

        assert deps.input_id == "story"
        assert deps.input_type == "user_story"


class TestDevinMcpTimeouts:
    def test_devin_toolset_is_built_with_timeouts(self):
        with patch.object(mcps, "MCPToolset") as mock_server, patch.object(
            mcps, "remove_disallowed_tools"
        ):
            mcps.McpsRegistry.ask_devin("agent", "project", MagicMock())

        kwargs = mock_server.call_args.kwargs
        assert kwargs["init_timeout"] == MCP_INIT_TIMEOUT_SECONDS
        assert kwargs["read_timeout"] == MCP_READ_TIMEOUT_SECONDS

    def test_devin_toolset_hides_the_wiki_contents_tool(self):
        with patch.object(mcps, "MCPToolset") as mock_server, patch.object(
            mcps, "remove_disallowed_tools"
        ) as mock_remove:
            mcps.McpsRegistry.ask_devin("agent", "project", MagicMock())

        mock_remove.assert_called_once_with(
            mock_server.return_value, ["read_wiki_contents"]
        )
