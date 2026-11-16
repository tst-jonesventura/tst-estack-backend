"""A turn has a hard ceiling on how much source code it may read.

Measured over 20 real tutor turns: a median of 42 tool calls and a worst case of
159, with 87% of them get_file_contents. Users reported every agent that reaches
ask_git — the tutor and the dev-steps agent above all — as very slow.

The counters live on AgentDeps, which ask_git hands to its sub-agent, so a budget
set here caps the whole turn instead of one agent at a time.
"""

import time
from unittest.mock import MagicMock

import pytest

from agents.utils.agent_deps import SOURCE_TOOL_BUDGET, AgentDeps
from agents.utils.agent_factory import TOOL_BUDGET_SPENT_MESSAGE, LoggingToolset


def _deps():
    return AgentDeps(
        project_id="p", user_id="u", agent_logger=MagicMock(), session=MagicMock()
    )


class _Wrapped:
    def __init__(self):
        self.calls = 0

    async def call_tool(self, name, tool_args, ctx, tool):
        self.calls += 1
        return {"content": "codigo"}


def _ctx(deps):
    ctx = MagicMock()
    ctx.deps = deps
    return ctx


class TestBudgetIsOnByDefault:
    def test_a_run_starts_with_the_source_budget(self):
        assert _deps().custom_usage_tool_limit == SOURCE_TOOL_BUDGET

    def test_the_budget_is_not_shared_between_runs(self):
        first = _deps()
        first.custom_usage_tool_limit["get_file_contents"] = 1

        assert _deps().custom_usage_tool_limit == SOURCE_TOOL_BUDGET

    @pytest.mark.parametrize(
        "tool", ["get_file_contents", "search_code", "ask_git"]
    )
    def test_the_expensive_tools_are_capped(self, tool):
        assert _deps().custom_usage_tool_limit[tool] > 0

    def test_the_worst_case_is_still_bounded(self):
        """A cap of 25 cut off a sound investigation; this only bounds runaway."""
        assert SOURCE_TOOL_BUDGET["get_file_contents"] < 159


class TestEnforcement:
    @pytest.mark.anyio
    async def test_calls_stop_once_the_budget_is_spent(self):
        deps = _deps()
        deps.custom_usage_tool_limit["get_file_contents"] = 2
        wrapped = _Wrapped()
        toolset = LoggingToolset(wrapped)

        for _ in range(5):
            await toolset.call_tool("get_file_contents", {}, _ctx(deps), None)

        assert wrapped.calls == 2

    @pytest.mark.anyio
    async def test_the_model_is_told_why_and_what_to_do(self):
        deps = _deps()
        deps.custom_usage_tool_limit["get_file_contents"] = 1
        toolset = LoggingToolset(_Wrapped())

        await toolset.call_tool("get_file_contents", {}, _ctx(deps), None)
        blocked = await toolset.call_tool("get_file_contents", {}, _ctx(deps), None)

        assert "responda agora com o que já reuniu" in blocked
        assert "get_file_contents" in blocked

    @pytest.mark.anyio
    async def test_the_refusal_is_not_a_silent_none(self):
        deps = _deps()
        deps.custom_usage_tool_limit["search_code"] = 0
        toolset = LoggingToolset(_Wrapped())

        blocked = await toolset.call_tool("search_code", {}, _ctx(deps), None)

        assert blocked is not None
        assert blocked == TOOL_BUDGET_SPENT_MESSAGE.format(
            name="search_code", limit=0
        )

    @pytest.mark.anyio
    async def test_an_uncapped_tool_is_never_blocked(self):
        deps = _deps()
        wrapped = _Wrapped()
        toolset = LoggingToolset(wrapped)

        for _ in range(30):
            await toolset.call_tool("get_stories", {}, _ctx(deps), None)

        assert wrapped.calls == 30

    @pytest.mark.anyio
    async def test_a_sub_agent_spends_the_same_turn_budget(self):
        """ask_git passes the parent's deps down, so the ceiling is shared."""
        deps = _deps()
        deps.custom_usage_tool_limit["get_file_contents"] = 3
        parent, child = _Wrapped(), _Wrapped()

        for _ in range(2):
            await LoggingToolset(parent).call_tool(
                "get_file_contents", {}, _ctx(deps), None
            )
        for _ in range(3):
            await LoggingToolset(child).call_tool(
                "get_file_contents", {}, _ctx(deps), None
            )

        assert parent.calls == 2
        assert child.calls == 1


class TestWritersKeepTheirOwnLimit:
    def test_setting_code_git_preserves_the_source_budget(self):
        deps = _deps()

        deps.custom_usage_tool_limit["code_git"] = 1

        assert deps.custom_usage_tool_limit["code_git"] == 1
        assert (
            deps.custom_usage_tool_limit["get_file_contents"]
            == SOURCE_TOOL_BUDGET["get_file_contents"]
        )



class _SlowLogger:
    def __init__(self, seconds):
        self.seconds = seconds
        self.agent_run_id = "run"

    def log(self, message, is_finished=False):
        time.sleep(self.seconds)


class TestToolTiming:
    """Each call reports how long the progress log took and how long the tool took.

    Dev-steps runs spend ~6s between tool calls, of which a benchmark attributes
    only ~3s to the model. The rest is either the tool or the synchronous progress
    log, which writes to the database and notifies every project member.
    """

    @pytest.mark.anyio
    async def test_a_call_reports_its_timing(self, caplog):
        deps = _deps()
        toolset = LoggingToolset(_Wrapped())

        with caplog.at_level("INFO"):
            await toolset.call_tool("get_file_contents", {}, _ctx(deps), None)

        timing = [r.getMessage() for r in caplog.records if "tool_timing" in r.getMessage()]
        assert len(timing) == 1
        assert "tool=get_file_contents" in timing[0]

    @pytest.mark.anyio
    async def test_the_log_write_is_measured_separately(self, caplog):
        deps = _deps()
        deps.agent_logger = _SlowLogger(0.2)
        toolset = LoggingToolset(_Wrapped())

        with caplog.at_level("INFO"):
            await toolset.call_tool("get_file_contents", {}, _ctx(deps), None)

        timing = next(r for r in caplog.records if "tool_timing" in r.getMessage())
        log_seconds = float(timing.getMessage().split("log_s=")[1].split()[0])
        assert log_seconds >= 0.2

    @pytest.mark.anyio
    async def test_timing_is_reported_even_when_the_tool_fails(self, caplog):
        class _Failing:
            async def call_tool(self, name, tool_args, ctx, tool):
                raise RuntimeError("github fora do ar")

        toolset = LoggingToolset(_Failing())
        ctx = _ctx(_deps())
        caplog.set_level("INFO")

        with pytest.raises(RuntimeError):
            await toolset.call_tool("get_file_contents", {}, ctx, None)

        assert any("tool_timing" in r.getMessage() for r in caplog.records)

    @pytest.mark.anyio
    async def test_a_blocked_call_is_not_timed(self, caplog):
        deps = _deps()
        deps.custom_usage_tool_limit["search_code"] = 0
        toolset = LoggingToolset(_Wrapped())

        with caplog.at_level("INFO"):
            await toolset.call_tool("search_code", {}, _ctx(deps), None)

        assert not any("tool_timing" in r.getMessage() for r in caplog.records)
