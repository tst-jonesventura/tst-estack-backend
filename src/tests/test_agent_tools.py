"""Tests for ToolsRegistry: an unconfigured MCP source must degrade into an
explanatory answer instead of aborting the agent run."""

import threading
import time
import uuid
from unittest.mock import MagicMock, patch

from agents.utils import agent_factory, tools as tools_module
from agents.utils.agent_deps import AgentDeps
from agents.utils.mcp_providers import (
    McpConfigNotFoundError,
    McpConnectionError,
    McpProviderNotConfiguredError,
)
from agents.utils.tools import ToolsRegistry
from database.models import DocumentTechSpec


def _ctx(db_session, project_id="proj-1", user_id="user-1"):
    deps = AgentDeps(
        project_id=project_id,
        user_id=user_id,
        agent_logger=MagicMock(),
        session=db_session,
    )
    ctx = MagicMock()
    ctx.deps = deps
    return ctx


def _patch_session(db_session):
    session_cm = MagicMock()
    session_cm.__enter__.return_value = db_session
    session_cm.__exit__.return_value = False
    return patch.object(tools_module, "create_session", return_value=session_cm)


class TestAskGitDegradation:
    def _run_with_mcp_error(self, db_session, error):
        ctx = _ctx(db_session)
        with _patch_session(db_session), patch.object(
            agent_factory, "create_agent_from_database", side_effect=error
        ):
            return ToolsRegistry.ask_git(ctx, "Como funciona a autenticação?")

    def test_provider_not_configured_returns_message_instead_of_raising(
        self, db_session
    ):
        result = self._run_with_mcp_error(
            db_session, McpProviderNotConfiguredError("no provider for 'scm'")
        )

        assert "não está disponível" in result
        assert "código-fonte" in result

    def test_message_tells_the_model_not_to_retry(self, db_session):
        result = self._run_with_mcp_error(
            db_session, McpConfigNotFoundError("credentials missing")
        )

        assert "Não tente chamar esta ferramenta novamente" in result

    def test_connection_error_also_degrades(self, db_session):
        result = self._run_with_mcp_error(db_session, McpConnectionError("boom"))

        assert "não está disponível" in result

    def test_underlying_reason_is_surfaced(self, db_session):
        result = self._run_with_mcp_error(
            db_session, McpProviderNotConfiguredError("no provider for 'scm'")
        )

        assert "no provider for 'scm'" in result

    def test_returns_agent_output_when_source_is_configured(self, db_session):
        ctx = _ctx(db_session)
        agent = MagicMock()
        agent.run_sync.return_value = MagicMock(output="A autenticação usa JWT.")

        with _patch_session(db_session), patch.object(
            agent_factory, "create_agent_from_database", return_value=agent
        ):
            result = ToolsRegistry.ask_git(ctx, "Como funciona a autenticação?")

        assert result == "A autenticação usa JWT."

    def test_unexpected_errors_still_propagate(self, db_session):
        ctx = _ctx(db_session)
        with _patch_session(db_session), patch.object(
            agent_factory,
            "create_agent_from_database",
            side_effect=ValueError("agent 'ask_git' not found"),
        ):
            try:
                ToolsRegistry.ask_git(ctx, "pergunta")
            except ValueError as exc:
                assert "not found" in str(exc)
            else:  # pragma: no cover - guards against silent over-catching
                raise AssertionError("ValueError should not be swallowed")


class TestAskDatabase:
    def test_project_without_database_degrades_into_a_message(self, db_session):
        ctx = _ctx(db_session)
        with _patch_session(db_session), patch.object(
            agent_factory,
            "create_agent_from_database",
            side_effect=McpConfigNotFoundError("Nenhum banco de dados ativo"),
        ):
            result = ToolsRegistry.ask_database(ctx, "Quais tabelas guardam pedidos?")

        assert "banco de dados" in result
        assert "Não tente chamar esta ferramenta novamente" in result
        assert "Nenhum banco de dados ativo" in result
        ctx.deps.agent_logger.log.assert_any_call(
            "Banco de dados não está conectado para este projeto"
        )

    def test_delegates_to_the_ask_database_agent(self, db_session):
        ctx = _ctx(db_session)
        agent = MagicMock()
        agent.run_sync.return_value = MagicMock(output="pedido 1:N item_pedido")

        with _patch_session(db_session), patch.object(
            agent_factory, "create_agent_from_database", return_value=agent
        ) as create:
            result = ToolsRegistry.ask_database(ctx, "Como pedido se liga a item?")

        assert result == "pedido 1:N item_pedido"
        assert create.call_args.kwargs["name"] == "ask_database"
        limits = agent.run_sync.call_args.kwargs["usage_limits"]
        assert limits.tool_calls_limit == tools_module.SOURCE_AGENT_TOOL_CALLS_LIMIT

    def test_sources_are_not_barriers_so_they_run_in_parallel(self):
        assert "ask_database" not in agent_factory.SEQUENTIAL_TOOLS
        assert "ask_git" not in agent_factory.SEQUENTIAL_TOOLS
        assert "code_git" in agent_factory.SEQUENTIAL_TOOLS

    def test_connection_is_released_before_the_sub_agent_runs(self, db_session):
        ctx = _ctx(db_session)
        session_cm = MagicMock()
        session_cm.__enter__.return_value = db_session
        session_cm.__exit__.return_value = False
        agent = MagicMock()
        agent.run_sync.side_effect = lambda *args, **kwargs: (
            session_cm.__exit__.assert_called_once() or MagicMock(output="ok")
        )

        with patch.object(tools_module, "create_session", return_value=session_cm), patch.object(
            agent_factory, "create_agent_from_database", return_value=agent
        ):
            assert ToolsRegistry.ask_database(ctx, "Quais tabelas existem?") == "ok"


class TestSourceConcurrency:
    def _deps(self):
        return AgentDeps(project_id="p", user_id="u", session=MagicMock())

    def test_different_sources_overlap(self):
        deps = self._deps()
        inside = threading.Barrier(2, timeout=2)

        def ask(source):
            with deps.source_turn(source):
                inside.wait()

        threads = [threading.Thread(target=ask, args=(source,)) for source in ("ask_git", "ask_database")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)

        assert not inside.broken

    def test_the_same_source_runs_one_call_at_a_time(self):
        deps = self._deps()
        active, peak = [0], [0]
        guard = threading.Lock()

        def ask():
            with deps.source_turn("ask_database"):
                with guard:
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                time.sleep(0.05)
                with guard:
                    active[0] -= 1

        threads = [threading.Thread(target=ask) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)

        assert peak[0] == 1

    def test_budget_is_not_overspent_by_concurrent_calls(self):
        deps = self._deps()
        deps.custom_usage_tool_limit = {"ask_database": 3}
        granted = []

        def use():
            granted.append(deps.try_use_tool("ask_database"))

        threads = [threading.Thread(target=use) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)

        assert granted.count(True) == 3
        assert deps.used_tools["ask_database"] == 3


class TestGetTechSpecs:
    def _spec(self, db_session, project, user, title):
        spec = DocumentTechSpec(
            id=str(uuid.uuid4()),
            title=title,
            content="conteudo",
            project_id=project.id,
            user_id=user.id,
            log="",
        )
        db_session.add(spec)
        db_session.commit()
        db_session.refresh(spec)
        return spec

    def test_reports_every_id_already_fetched(
        self, db_session, sample_project, sample_user
    ):
        spec = self._spec(db_session, sample_project, sample_user, "Autenticacao")
        ctx = _ctx(db_session)
        ctx.deps.fetched_ids.add(spec.id)

        result = ToolsRegistry.get_tech_specs(ctx, [spec.id])

        assert spec.id in result
        assert "já foi buscada anteriormente" in result

    def test_mixes_already_fetched_notice_with_fresh_content(
        self, db_session, sample_project, sample_user
    ):
        seen = self._spec(db_session, sample_project, sample_user, "Ja vista")
        fresh = self._spec(db_session, sample_project, sample_user, "Nova")
        ctx = _ctx(db_session)
        ctx.deps.fetched_ids.add(seen.id)

        result = ToolsRegistry.get_tech_specs(ctx, [seen.id, fresh.id])

        assert "já foi buscada anteriormente" in result
        assert "Nova" in result

    def test_never_returns_empty_for_a_known_id(
        self, db_session, sample_project, sample_user
    ):
        spec = self._spec(db_session, sample_project, sample_user, "Qualquer")
        ctx = _ctx(db_session)
        ctx.deps.fetched_ids.add(spec.id)

        assert ToolsRegistry.get_tech_specs(ctx, [spec.id]).strip() != ""

    def test_empty_id_list_is_rejected(self, db_session):
        ctx = _ctx(db_session)

        assert "Nenhum ID" in ToolsRegistry.get_tech_specs(ctx, [])


class TestSourcesRunInParallelInARealRun:
    def test_ask_git_and_ask_database_overlap(self):
        from pydantic_ai import Agent, Tool
        from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
        from pydantic_ai.models.function import AgentInfo, FunctionModel

        both_started = threading.Barrier(2, timeout=5)

        def slow(name):
            def call(question: str) -> str:
                both_started.wait()
                return f"{name}: ok"

            call.__name__ = name
            return call

        def model(messages, info: AgentInfo) -> ModelResponse:
            if len(messages) == 1:
                return ModelResponse(
                    parts=[
                        ToolCallPart("ask_git", {"question": "regra de frete"}),
                        ToolCallPart("ask_database", {"question": "regra de frete"}),
                    ]
                )
            return ModelResponse(parts=[TextPart("pronto")])

        agent = Agent(
            FunctionModel(model),
            tools=[
                Tool(slow(name), sequential=name in agent_factory.SEQUENTIAL_TOOLS)
                for name in ("ask_git", "ask_database")
            ],
        )
        agent.run_sync("Qual a regra de frete?")

        assert not both_started.broken
