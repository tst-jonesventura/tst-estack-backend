import time
from dataclasses import dataclass
from typing import Any

from pydantic_ai import (
    Agent,
    CombinedToolset,
    FunctionToolset,
    RunContext,
    Tool,
    ToolsetTool,
    WrapperToolset,
)
from pydantic_ai.settings import ModelSettings
from sqlalchemy.orm import Session, joinedload

import utils.log as log
from agents.utils.agent_deps import AgentDeps
from agents.utils.agent_run_utils import AgentLogger
from agents.utils.context import CONTEXT_DICT
from agents.utils.mcp_providers import McpConfigurationError
from agents.utils.mcps import MCPS_DICT
from agents.utils.tools import TOOLS_DICT
from database import models
from utils.ttl_cache import TTLCache

logger = log.setup_custom_logger(__name__)

# Short enough that an admin editing an agent sees the change without a restart, long
# enough that a burst of ask_git calls does not re-read the agent graph each time.
AGENT_SPEC_TTL_SECONDS = 60

SEQUENTIAL_TOOLS = ("code_git",)

MCP_UNAVAILABLE_LOG = (
    "Repositório de código indisponível; respondendo sem consultar o código"
)

REFERENCE_INSTRUCTIONS = """\
## Como citar especificações e detalhamentos técnicos
- Em todo texto que você escrever (respostas, descrições, critérios, detalhamentos \
técnicos, cenários, mensagens), cite uma especificação (história) sempre pelo título, \
entre aspas, por exemplo "Cadastro de clientes". Nunca escreva o id, nem o id junto \
com o título.
- O mesmo vale para detalhamentos técnicos: cite sempre pelo título.
- O id serve só para chamar ferramentas (como `get_stories` e `get_tech_specs`) e para \
preencher campos estruturados que pedem explicitamente o id ou UUID.
"""

TOOL_BUDGET_SPENT_MESSAGE = (
    "Você já usou a ferramenta '{name}' o número máximo de vezes permitido nesta "
    "execução ({limit}) e ela não será executada de novo. Não insista nem tente "
    "contornar: responda agora com o que já reuniu e diga explicitamente o que não "
    "conseguiu verificar."
)


@dataclass(frozen=True)
class AgentSpec:
    """Snapshot of an agent's stored configuration.

    Detached ORM objects are not safe to share across sessions and threads, so the
    cache holds this plain value instead.
    """

    id: str
    name: str
    model: str
    system_prompt: str | None
    user_prompt: str | None
    temperature: float | None
    max_retries: int | None
    tool_names: tuple[str, ...]
    context_names: tuple[str, ...]
    mcp_names: tuple[str, ...]


_agent_spec_cache: TTLCache[AgentSpec] = TTLCache(AGENT_SPEC_TTL_SECONDS)


def _to_spec(agent_info: models.Agent) -> AgentSpec:
    return AgentSpec(
        id=agent_info.id,
        name=agent_info.name,
        model=agent_info.model,
        system_prompt=agent_info.system_prompt,
        user_prompt=agent_info.user_prompt,
        temperature=agent_info.temperature,
        max_retries=agent_info.max_retries,
        tool_names=tuple(t.tool_name for t in agent_info.tools),
        context_names=tuple(c.context_name for c in agent_info.context),
        mcp_names=tuple(m.mcp_name for m in agent_info.mcps),
    )


def get_agent_spec(
    session: Session, agent_name: str, project_id: str | None
) -> AgentSpec | None:
    cache_key = (agent_name, project_id)
    cached = _agent_spec_cache.get(cache_key)
    if cached is not None:
        return cached

    agent_info = get_agent_info(
        session=session, agent_name=agent_name, project_id=project_id
    )
    if agent_info is None:
        return None

    spec = _to_spec(agent_info)
    _agent_spec_cache.put(cache_key, spec)
    return spec


def _joinedload_agent_graph(query):
    return query.options(
        joinedload(models.Agent.tools),
        joinedload(models.Agent.context),
        joinedload(models.Agent.mcps),
    )


def get_default_agent_info_by_name(
        session: Session, agent_name: str
) -> models.Agent | None:
    """
    Return the default Agent for a given name.

    Backwards compatibility:
    - If there is at least one agent with is_default=True for this name, use the
      newest of those.
    - If there is NO explicit default, fall back to the pre-change behaviour and
      return the newest agent by name (ignoring is_default).
    """
    base_query = session.query(models.Agent).filter(models.Agent.name == agent_name)
    base_query = _joinedload_agent_graph(base_query)

    # Prefer explicit defaults if present
    default_query = base_query.filter(models.Agent.is_default == True)  # noqa: E712
    default_agent = default_query.order_by(models.Agent.created_at.desc()).scalar()
    if default_agent:
        return default_agent

    # Fallback: behave like original implementation (newest by created_at)
    return base_query.order_by(models.Agent.created_at.desc()).scalar()


def get_project_agent_override(
        session: Session, project_id: str, agent_name: str
) -> models.Agent | None:
    override = (
        session.query(models.ProjectAgent)
        .filter(
            models.ProjectAgent.project_id == project_id,
            models.ProjectAgent.agent_name == agent_name,
        )
        .options(joinedload(models.ProjectAgent.agent))
        .one_or_none()
    )
    if not override:
        return None

    # override.agent is loaded, but we still want tools/context/mcps
    query = session.query(models.Agent).filter(models.Agent.id == override.agent_id)
    query = _joinedload_agent_graph(query)
    return query.scalar()


def get_agent_info(
        session: Session, agent_name: str, project_id: str | None = None
) -> models.Agent | None:
    """
    Resolve agent by name for a given project:
    1) If project override exists for (project_id, agent_name), return it
    2) Else return default Agent (is_default=true) for the name (newest)
    """
    logger.info("Resolving agent info for agent name %s", agent_name)
    if project_id:
        overridden = get_project_agent_override(session, project_id, agent_name)
        if overridden:
            return overridden
    return get_default_agent_info_by_name(session, agent_name)


def get_agent_info_by_name(session: Session, agent_name: str) -> models.Agent | None:
    """
    Backwards compatible: keep old name, but now resolves via defaults (no project).
    """
    return get_agent_info(session=session, agent_name=agent_name, project_id=None)


class LoggingToolset(WrapperToolset[AgentDeps]):
    async def call_tool(
            self,
            name: str,
            tool_args: dict[str, Any],
            ctx: RunContext[AgentDeps],
            tool: ToolsetTool[AgentDeps],
    ) -> Any:
        logger.info(f"MCP tool called: {name} with args: {tool_args}")
        log_seconds = 0.0

        if ctx.deps:
            if not ctx.deps.try_use_tool(name):
                ctx.deps.agent_logger.log(f"Limite de uso da ferramenta '{name}' atingido")
                logger.warning(f"Tool usage limit reached for: {name}")
                return TOOL_BUDGET_SPENT_MESSAGE.format(
                    name=name, limit=ctx.deps.custom_usage_tool_limit.get(name)
                )

            if ctx.deps.agent_logger:
                log_started = time.perf_counter()
                ctx.deps.agent_logger.log(f"Chamando ferramenta '{name}'")
                log_seconds = time.perf_counter() - log_started

        tool_started = time.perf_counter()
        try:
            return await super().call_tool(name, tool_args, ctx, tool)
        finally:
            logger.info(
                "tool_timing tool=%s log_s=%.2f tool_s=%.2f",
                name,
                log_seconds,
                time.perf_counter() - tool_started,
            )


def build_model_settings(agent_info: "AgentSpec") -> ModelSettings | None:
    """Model settings from the stored agent config.

    ``temperature`` is only sent when explicitly configured: reasoning models such
    as the gpt-5 family reject it.
    """
    if agent_info.temperature is None:
        return None
    return ModelSettings(temperature=agent_info.temperature)


def memoized_context(context_name: str, context_fn):
    """Evaluate a context function once per run instead of once per model request."""

    def instruction(ctx: RunContext[AgentDeps]) -> str:
        cache = ctx.deps.context_cache
        if context_name not in cache:
            cache[context_name] = context_fn(ctx)
        return cache[context_name]

    return instruction


def build_mcp_toolsets(
    agent_name: str,
    project_id: str,
    session: Session,
    mcp_names: tuple[str, ...],
    agent_logger: AgentLogger | None,
    optional: bool,
) -> list:
    """Build the agent's MCP toolsets, skipping unavailable ones when optional.

    A conversational agent should still answer from specs and stories when the
    repository cannot be reached; an agent that writes code should fail instead.
    """
    toolsets = []
    for mcp_name in mcp_names:
        try:
            toolsets.append(MCPS_DICT[mcp_name](agent_name, project_id, session))
        except McpConfigurationError as exc:
            if not optional:
                raise
            logger.warning(
                "MCP '%s' unavailable for agent '%s' (project %s): %s",
                mcp_name,
                agent_name,
                project_id,
                exc,
            )
            if agent_logger:
                agent_logger.log(MCP_UNAVAILABLE_LOG)
    return toolsets


def create_agent_from_database(
        name: str,
        session: Session,
        project_id: str,
        agent_logger: AgentLogger | None,
        output_type: Any = str,
        optional_mcps: bool = False,
) -> Agent[AgentDeps]:
    agent_info = get_agent_spec(
        session=session, agent_name=name, project_id=project_id
    )
    if not agent_info:
        logger.warning(
            "Agent '%s' not found. Looked for project override (project_id=%s) then defaults (is_default=true).",
            name,
            project_id,
        )
        if agent_logger:
            agent_logger.finish_agent_run_with_usage(
                f"Agent with name '{name}' not found for project '{project_id}' and no default exists.", None, None)
        raise ValueError(
            f"Agent with name '{name}' not found for project '{project_id}' and no default exists."
        )

    if agent_logger and getattr(agent_logger, "agent_run_id", None):
        agent_run = (
            session.query(models.AgentRun)
            .filter(models.AgentRun.id == agent_logger.agent_run_id)
            .first()
        )
        if agent_run and not agent_run.agent_id:
            agent_run.agent_id = agent_info.id
            session.commit()

    tools = [
        Tool(TOOLS_DICT[tool_name], sequential=tool_name in SEQUENTIAL_TOOLS)
        for tool_name in agent_info.tool_names
    ]
    function_toolset = FunctionToolset(tools=tools)

    mcp_toolsets = build_mcp_toolsets(
        name,
        project_id,
        session,
        agent_info.mcp_names,
        agent_logger,
        optional=optional_mcps,
    )
    toolsets = CombinedToolset([function_toolset] + mcp_toolsets)

    logger.info(
        f"Creating agent '{name}' with tools {list(agent_info.tool_names)} and mcps {list(agent_info.mcp_names)}"
    )

    loggable_toolsets = LoggingToolset(toolsets)
    system_prompt_context = [
        memoized_context(context_name, CONTEXT_DICT[context_name])
        for context_name in agent_info.context_names
    ]

    instructions = [
                       prompt
                       for prompt in (agent_info.user_prompt, agent_info.system_prompt)
                       if prompt is not None
                   ] + [REFERENCE_INSTRUCTIONS] + system_prompt_context

    agent = Agent(
        model=agent_info.model,
        instructions=instructions,
        output_type=output_type,
        deps_type=AgentDeps,
        toolsets=[loggable_toolsets],
        name=name,
        retries=agent_info.max_retries or 1,
        model_settings=build_model_settings(agent_info),
    )
    return agent
