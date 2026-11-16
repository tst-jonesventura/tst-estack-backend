import inspect

from agents.utils.agent_deps import AgentDeps
from agents.utils.mcp_providers import McpConfigurationError
from database import models
from database.database import create_session
from pydantic_ai import RunContext, UsageLimits
from schemas.tech_spec_schemas import DocumentTechSpecPublic
from schemas.user_story_schemas import UserStoryPublicWithTags
from sqlalchemy.orm import Session
from utils import log

logger = log.setup_custom_logger(__name__)

AGENT_LOGGER_WARNING = "Agent logger is not available in the dependencies. Logging will be skipped for this operation."


def _source_unavailable_message(source_label: str, reason: str) -> str:
    """Message returned to the model when a knowledge source is not configured."""
    return (
        f"A fonte '{source_label}' não está disponível para este projeto: {reason} "
        "Não tente chamar esta ferramenta novamente nesta conversa. "
        "Informe a limitação ao usuário e siga respondendo com as demais fontes "
        "disponíveis, sem inventar informações."
    )


SOURCE_AGENT_TOOL_CALLS_LIMIT = 50


def _ask_source_agent(
    ctx: RunContext[AgentDeps],
    agent_name: str,
    question: str,
    source_label: str,
    unavailable_log: str,
) -> str:
    """Delegate a question to the sub-agent that reads one knowledge source."""
    from agents.utils.agent_factory import create_agent_from_database

    logger.info(f"{agent_name} tool called with question: {question}")
    logger.info(f"Creating {agent_name} agent for user {ctx.deps.user_id}")
    logger.info(f"Project ID: {ctx.deps.project_id}")
    if not ctx.deps.agent_logger:
        logger.warning(AGENT_LOGGER_WARNING)
    else:
        ctx.deps.agent_logger.log(f"questão: {question}'")
    with create_session() as session:
        try:
            source_agent = create_agent_from_database(
                project_id=ctx.deps.project_id,
                name=agent_name,
                session=session,
                output_type=str,
                agent_logger=ctx.deps.agent_logger,
            )
        except McpConfigurationError as exc:
            logger.warning(
                "%s source unavailable for project %s: %s",
                agent_name,
                ctx.deps.project_id,
                exc,
            )
            if ctx.deps.agent_logger:
                ctx.deps.agent_logger.log(unavailable_log)
            return _source_unavailable_message(source_label, str(exc))

    with ctx.deps.source_turn(agent_name):
        result = source_agent.run_sync(
            question,
            deps=ctx.deps,
            usage_limits=UsageLimits(tool_calls_limit=SOURCE_AGENT_TOOL_CALLS_LIMIT),
        )
    return result.output


class ToolsRegistry:
    @staticmethod
    def get_stories(ctx: RunContext[AgentDeps], story_ids: list[str]) -> str:
        """Fetch UserStories by a list of IDs.
        Never call get_stories multiple times with the same story_id: If you updated a story with a tool, the tool will return the updated version.
        This tool should be called with at least one story_id that has not been fetched before.
        Never call this tool with an empty list.
        Do not guess or invent story IDs; only use valid IDs from the project.

        """
        # remove already fetched ids and track already fetched ids
        if not story_ids:
            return "Nenhum ID de história fornecido."
        stories_str = ""
        filtered_story_ids = []
        for sid in story_ids:
            if sid in ctx.deps.fetched_ids:
                stories_str += f"História com ID {sid} já foi buscada anteriormente.\n"
            else:
                filtered_story_ids.append(sid)
        ctx.deps.fetched_ids.update(filtered_story_ids)
        if not ctx.deps.agent_logger:
            logger.warning(AGENT_LOGGER_WARNING)
        else:
            ctx.deps.agent_logger.log("Examinando histórias existentes")
        with ctx.deps.db() as session:
            stories = (
                session.query(models.UserStory)
                .filter(models.UserStory.id.in_(filtered_story_ids))
                .all()
            )
            for story in stories:
                stories_str += (
                    UserStoryPublicWithTags.model_validate(story).model_dump_json(
                        indent=2
                    )
                    + "\n"
                )

            return stories_str

    @staticmethod
    def get_tech_specs(ctx: RunContext[AgentDeps], spec_ids: list[str]) -> str:
        """Fetch DocumentTechSpecs by a list of IDs.
        This tool should be called with at least one spec_id that has not been fetched before.
        Never call this tool with an empty list.
        Do not guess or invent spec IDs; only use valid IDs from the project.
        Never call get_tech_specs multiple times with the same spec_id: If you updated a tech spec with a tool, the tool will return the updated version.
        """
        if not spec_ids:
            return "Nenhum ID de especificação técnica fornecido."

        if not ctx.deps.agent_logger:
            logger.warning(AGENT_LOGGER_WARNING)
        else:
            ctx.deps.agent_logger.log("Examinando especificações técnicas existentes")
        filtered_spec_ids = []
        specs_str = ""
        for sid in spec_ids:
            if sid in ctx.deps.fetched_ids:
                specs_str += f"Especificação técnica com ID {sid} já foi buscada anteriormente.\n"
            else:
                filtered_spec_ids.append(sid)
        ctx.deps.fetched_ids.update(filtered_spec_ids)
        with ctx.deps.db() as session:
            specs = (
                session.query(models.DocumentTechSpec)
                .filter(models.DocumentTechSpec.id.in_(filtered_spec_ids))
                .all()
            )
            for spec in specs:
                specs_str += (
                    DocumentTechSpecPublic.model_validate(spec).model_dump_json(
                        indent=2
                    )
                    + "\n"
                )
            return specs_str

    @staticmethod
    def ask_git(ctx: RunContext[AgentDeps], question: str) -> str:
        """Answers questions about a git project. The questions must be concise, clear and specific. Prioritize calling this tool multiple times with specific questions instead of a single call with many questions."""
        return _ask_source_agent(
            ctx,
            "ask_git",
            question,
            source_label="código-fonte (repositório)",
            unavailable_log="Repositório de código não está conectado para este projeto",
        )

    @staticmethod
    def ask_database(ctx: RunContext[AgentDeps], question: str) -> str:
        """Answers questions about the project's databases by reading their live structure: tables, columns, relationships, constraints, triggers, functions, views, security policies and what the data looks like (statistics and masked samples). Use it for the data model, entity relationships, business rules enforced by the database and where some information is stored. Ask concise, specific questions; prefer several focused calls over one broad question."""
        return _ask_source_agent(
            ctx,
            "ask_database",
            question,
            source_label="banco de dados",
            unavailable_log="Banco de dados não está conectado para este projeto",
        )

    @staticmethod
    async def code_git(ctx: RunContext[AgentDeps], user_message: str) -> str:
        from agents.utils.agent_factory import create_agent_from_database

        logger.info(f"code_git tool called with user_message: {user_message}")
        logger.info(f"Creating code_git agent for user {ctx.deps.user_id}")
        logger.info(f"Project ID: {ctx.deps.project_id}")

        if not ctx.deps.agent_logger:
            logger.warning(AGENT_LOGGER_WARNING)
        else:
            ctx.deps.agent_logger.log(f"mensagem do usuário: {user_message}'")

        with ctx.deps.db() as session:
            code_git_agent = create_agent_from_database(
                name="code_git",
                project_id=ctx.deps.project_id,
                session=session,
                output_type=str,
                agent_logger=ctx.deps.agent_logger,
            )

        response = await code_git_agent.run(
            user_message,
            deps=ctx.deps,
            output_type=str,
            usage_limits=UsageLimits(tool_calls_limit=50),
        )
        return response.output


_TOOLS_DICT = inspect.getmembers(ToolsRegistry, predicate=inspect.isfunction)
TOOLS_DICT = dict(_TOOLS_DICT)


def _sync_tools(session: Session):
    for name, func in TOOLS_DICT.items():
        tool_entry = session.query(models.Tool).filter_by(name=name).scalar()
        if tool_entry:
            tool_entry.name = name
            tool_entry.description = func.__doc__ or "No description available."
        else:
            tool_entry = models.Tool(
                name=name,
                description=func.__doc__ or "No description available.",
            )
        session.add(tool_entry)
    session.commit()


def sync_tools(session):
    if hasattr(session, "query"):
        _sync_tools(session)
    else:
        with session as s:
            _sync_tools(s)
