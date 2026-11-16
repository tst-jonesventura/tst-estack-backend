"""Conversational onboarding tutor agent.

Unlike the other e-Stack agents (one-shot, background processing), the tutor is a
multi-turn conversational agent. Each user is given a single persisted conversation per
project, so the tutor is strictly scoped to a single project (repositories, tech specs and
stories are always fetched using the conversation's ``project_id`` only).
"""

import asyncio
from datetime import datetime, timedelta, timezone

from pydantic_ai import Agent, RunUsage, UsageLimits
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import utils.log as log
from agents.mcp_servers.database_server import ENGINE_LABELS
from agents.utils.agent_deps import AgentDeps
from agents.utils.agent_factory import create_agent_from_database
from agents.utils.agent_run_utils import AgentLogger
from database import models
from database.database import create_session
from schemas.base import date_now

logger = log.setup_custom_logger(__name__)

TUTOR_AGENT_NAME = "tutor"

# A tutor turn whose run has shown no activity for this long is considered dead (the
# background task died, the dyno recycled mid-turn, or the reply was never persisted) and is
# reconciled to "failed" so the frontend stops polling forever. Measured against the newest
# AgentLog of the turn's run, so a slow-but-working turn is never reaped.
STALE_TURN_TIMEOUT = timedelta(minutes=4)

# Number of most recent messages kept verbatim in the model's message history.
# Older messages are folded into a concise rolling summary injected into the context.
MAX_HISTORY_MESSAGES = 20

# Lightweight model used only to keep the rolling conversation summary up to date.
SUMMARY_MODEL = "openai:gpt-4.1-nano"

TUTOR_OUTPUT_INSTRUCTIONS = """\
## Formatação da resposta
A interface renderiza Markdown (GFM) e diagramas Mermaid. HTML não é renderizado.

### Tabelas
- Use tabelas Markdown com linha de cabeçalho e linha separadora (`| --- |`), uma \
linha por registro, sem quebrar uma célula em várias linhas.
- Mantenha as células curtas. Para marcar sim/não use ✅, ❌ e ⚠️ e explique a \
legenda logo abaixo da tabela.
- Não use HTML dentro de tabelas.

### Diagramas
- Quando pedirem um diagrama, ou quando um fluxo, uma integração, a estrutura de \
classes ou o modelo de dados ficar mais claro visualmente, responda com um bloco \
```mermaid.
- Escolha o tipo pelo que precisa ser mostrado: integração e arquitetura com \
`flowchart LR`; troca de mensagens ao longo do tempo com `sequenceDiagram`; classes \
e entidades do código com `classDiagram`; modelo de dados com `erDiagram`; ciclo \
de vida com `stateDiagram-v2`.
- Para o diagrama desenhar sem erro:
  - um diagrama por bloco, começando pela declaração do tipo;
  - identificadores de nós sem espaços nem acentos (`BE`, `FrontendApp`), com o \
texto amigável no rótulo: `BE[Backend E-Stack]`;
  - rótulos com parênteses, dois-pontos, barras ou vírgulas entre aspas: \
`A["Lê credenciais (App ID)"]`;
  - nada de HTML nos rótulos, exceto `<br/>` para quebrar linha;
  - até cerca de 15 elementos; se for maior, divida em diagramas menores.
- Desenhe apenas o que foi verificado no código, nas especificações ou nos \
detalhamentos técnicos. Não invente componentes; se algo for suposição, diga no texto.
- Para o modelo de dados, quando `ask_database` estiver disponível, monte o \
`erDiagram` com as tabelas, colunas-chave e cardinalidades que ele retornar.
- Depois do diagrama, explique em poucas linhas o que ele mostra.
- Para árvores de pastas ou hierarquias simples, use um bloco ```text.
"""

TUTOR_SOURCES_INSTRUCTIONS = """\
## Fontes do projeto
Fontes configuradas neste projeto:
- Código: {code}
- Bancos de dados: {databases}
- Especificações e detalhamentos técnicos: no contexto; aprofunde com `get_stories` e \
`get_tech_specs`.

- Para perguntas sobre o projeto (regras de negócio, fluxos, dados, onde algo é feito, \
como algo funciona), consulte **todas** as fontes configuradas acima, mesmo que a \
pergunta não cite "código" ou "banco". Chame `ask_git` e `ask_database` na mesma \
rodada, para que rodem em paralelo, e cruze com as especificações e os detalhamentos \
técnicos relevantes.
- Não chame ferramentas de fontes que não estão configuradas.
- Limite-se a uma fonte só quando a pessoa pedir (ex.: "olhe só o código", "apenas no \
banco").
- Cumprimentos, combinação de próximos passos e perguntas sobre o próprio onboarding \
não precisam de consulta.
- Na resposta, diga de onde veio cada informação. Se as fontes se complementam ou \
divergem, apresente-as lado a lado (ex.: "o banco garante X pela constraint Y; o código \
faz Z em W"). Nunca descarte o que uma fonte encontrou: uma regra aplicada pelo banco \
vale para todo acesso aos dados.
- Se uma fonte não encontrou nada sobre o assunto, diga isso explicitamente (ex.: "não \
há regra de pontos no banco; ela deve estar no código").
"""


def _database_label(database) -> str:
    engine = ENGINE_LABELS.get(database.engine, database.engine)
    description = f": {database.description}" if database.description else ""
    return f"`{database.alias}` ({engine}){description}"


def tutor_sources_instructions(session: Session, project_id: str) -> str:
    """The project's configured knowledge sources and how the tutor must consult them."""
    repositories = (
        session.query(models.ProjectRepository.full_name)
        .filter(models.ProjectRepository.project_id == project_id)
        .order_by(models.ProjectRepository.full_name)
        .all()
    )
    databases = (
        session.query(
            models.ProjectDatabase.alias,
            models.ProjectDatabase.engine,
            models.ProjectDatabase.description,
        )
        .filter(
            models.ProjectDatabase.project_id == project_id,
            models.ProjectDatabase.is_active == True,
        )
        .order_by(models.ProjectDatabase.alias)
        .all()
    )
    code = (
        ", ".join(row.full_name for row in repositories) + " (consulte com `ask_git`)"
        if repositories
        else "nenhum repositório conectado"
    )
    database_list = (
        "; ".join(_database_label(database) for database in databases)
        + " (consulte com `ask_database`)"
        if databases
        else "nenhum banco cadastrado"
    )
    return TUTOR_SOURCES_INSTRUCTIONS.format(code=code, databases=database_list)


_SUMMARY_INSTRUCTIONS = (
    "Você resume conversas entre um tutor de onboarding e um usuário do e-Stack. "
    "Produza um resumo objetivo e conciso (no máximo 8 linhas), em português, que capture "
    "a linha de pensamento do usuário, o assunto que está sendo trabalhado, dúvidas já "
    "respondidas e próximos passos combinados. Não invente informações. "
    "Quando receber <resumo_anterior>, incorpore as <novas_mensagens> a ele e devolva um "
    "único resumo atualizado, preservando o que ainda for relevante e descartando o resto."
)


def get_or_create_conversation(
    session: Session, project_id: str, user_id: str
) -> models.TutorConversation:
    conversation = (
        session.query(models.TutorConversation)
        .filter_by(project_id=project_id, user_id=user_id)
        .one_or_none()
    )
    if conversation is not None:
        return conversation

    conversation = models.TutorConversation(project_id=project_id, user_id=user_id)
    session.add(conversation)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return (
            session.query(models.TutorConversation)
            .filter_by(project_id=project_id, user_id=user_id)
            .one()
        )
    session.refresh(conversation)
    return conversation


def replayable_messages(
    messages: list[models.TutorMessage],
) -> list[models.TutorMessage]:
    """Messages safe to send back to the model as conversation history."""
    return [
        message
        for message in messages
        if message.role in ("user", "assistant")
        and message.status == "completed"
        and (message.content or "").strip()
    ]


def _build_message_history(
    messages: list[models.TutorMessage],
) -> list[ModelMessage]:
    history: list[ModelMessage] = []
    for message in replayable_messages(messages):
        if message.role == "user":
            history.append(
                ModelRequest(parts=[UserPromptPart(content=message.content)])
            )
        else:
            history.append(ModelResponse(parts=[TextPart(content=message.content)]))
    return history


def _summarize_older_messages(
    older_messages: list[models.TutorMessage], previous_summary: str | None = None
) -> str | None:
    if not older_messages:
        return None
    transcript = "\n".join(
        f"{'Usuário' if m.role == 'user' else 'Tutor'}: {m.content}"
        for m in older_messages
    )
    if previous_summary:
        transcript = (
            "<resumo_anterior>\n"
            f"{previous_summary}\n"
            "</resumo_anterior>\n\n"
            "<novas_mensagens>\n"
            f"{transcript}\n"
            "</novas_mensagens>"
        )
    try:
        summarizer = Agent(model=SUMMARY_MODEL, instructions=_SUMMARY_INSTRUCTIONS)
        result = summarizer.run_sync(transcript)
        return result.output
    except Exception:  # pragma: no cover - summary is best-effort
        logger.exception("Failed to summarize tutor conversation")
        return None


def create_run(
    session: Session, project_id: str, user_id: str, user_input: str
) -> models.AgentRun:
    """Create the AgentRun that records tool usage and token cost for one turn.

    Notifications are suppressed (see ``AgentLogger(notify=False)``) so chat turns do not
    spam project members, and the run is flagged internal so it stays out of the processing
    feed while still counting towards token usage.
    """
    from agents.utils.agent_factory import get_agent_info

    agent_info = get_agent_info(
        session=session, agent_name=TUTOR_AGENT_NAME, project_id=project_id
    )
    agent_id = agent_info.id if agent_info else None

    agent_run = models.AgentRun(
        user_id=user_id,
        project_id=project_id,
        agent_id=agent_id,
        title="Tutor",
        status="running",
        input=user_input,
        is_internal=True,
    )
    session.add(agent_run)
    session.commit()
    session.refresh(agent_run)
    return agent_run


def _finish_run(
    agent_run_id: str,
    model: str | None,
    usage: RunUsage | None,
    status: str = "finished",
) -> None:
    """Persist token usage for the Admin token-consumption panel and close the run."""
    with create_session() as session:
        agent_run = (
            session.query(models.AgentRun)
            .filter(models.AgentRun.id == agent_run_id)
            .one()
        )
        agent_run.status = status
        agent_run.model = model
        if usage:
            agent_run.requests = usage.requests
            agent_run.tool_calls = usage.tool_calls
            agent_run.input_tokens = usage.input_tokens
            agent_run.output_tokens = usage.output_tokens
            agent_run.cache_read_tokens = usage.cache_read_tokens
            agent_run.cache_write_tokens = usage.cache_write_tokens
            agent_run.input_audio_tokens = usage.input_audio_tokens
            agent_run.cache_audio_read_tokens = usage.cache_audio_read_tokens
        session.commit()


FAILURE_MESSAGE = (
    "Desculpe, não consegui concluir sua solicitação agora. "
    "Tente novamente em instantes."
)


def _write_assistant_result(
    assistant_message_id: str, content: str, status: str
) -> None:
    """Persist the assistant reply in its own short-lived session.

    The session used to run the agent may be expired/invalidated by the time the turn ends
    (tools can close the shared deps session), which would turn a commit on a pre-fetched
    row into a silent no-op. Re-fetching and committing in a fresh session guarantees the
    UPDATE is emitted, so the placeholder never stays stuck in ``processing``.

    The placeholder may be gone if the user reset the conversation mid-turn; that is an
    expected race, so the reply is simply dropped.
    """
    with create_session() as session:
        message = (
            session.query(models.TutorMessage)
            .filter(models.TutorMessage.id == assistant_message_id)
            .one_or_none()
        )
        if message is None:
            logger.info(
                "Tutor placeholder %s no longer exists (conversation reset); "
                "discarding the reply.",
                assistant_message_id,
            )
            return
        message.content = content
        message.status = status
        session.commit()


def _as_utc(value: datetime) -> datetime:
    """Timestamps come back naive from SQL Server and SQLite even on tz-aware columns."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def last_activity_at(session: Session, message: models.TutorMessage) -> datetime:
    """When the turn last showed a sign of life.

    The AgentLogger writes an AgentLog row at every tool boundary, so the newest log of
    the linked run is a genuine heartbeat. A turn with no logs yet has only its own
    creation time to go on.
    """
    if not message.agent_run_id:
        return _as_utc(message.created_at)
    latest = (
        session.query(func.max(models.AgentLog.timestamp))
        .filter(models.AgentLog.agent_run_id == message.agent_run_id)
        .scalar()
    )
    return _as_utc(latest or message.created_at)


def fail_stale_processing_messages(
    session: Session, conversation_id: str
) -> bool:
    """Reconcile dead turns: mark assistant placeholders whose run went silent past
    ``STALE_TURN_TIMEOUT`` as ``failed``. Called on read/write of the conversation so a
    dead turn is self-healed on the next poll instead of polling forever. Returns whether
    anything was reconciled.
    """
    cutoff = date_now() - STALE_TURN_TIMEOUT
    candidates = (
        session.query(models.TutorMessage)
        .filter(
            models.TutorMessage.conversation_id == conversation_id,
            models.TutorMessage.role == "assistant",
            models.TutorMessage.status == "processing",
            models.TutorMessage.created_at < cutoff,
        )
        .all()
    )
    stale_messages = [
        message
        for message in candidates
        if last_activity_at(session, message) < cutoff
    ]
    if not stale_messages:
        return False

    stale_run_ids = {m.agent_run_id for m in stale_messages if m.agent_run_id}
    for message in stale_messages:
        message.content = FAILURE_MESSAGE
        message.status = "failed"
    if stale_run_ids:
        (
            session.query(models.AgentRun)
            .filter(
                models.AgentRun.id.in_(stale_run_ids),
                models.AgentRun.status == "running",
            )
            .update({models.AgentRun.status: "failed"}, synchronize_session=False)
        )
    session.commit()
    return True


def process_tutor_turn(
    project_id: str,
    user_id: str,
    user_message: str,
    user_message_id: str,
    assistant_message_id: str,
    agent_run_id: str,
) -> None:
    """Run a single tutor turn in the background and update the placeholder reply.

    The AgentRun, the user message and an assistant placeholder (``status="processing"``)
    are persisted synchronously by the router *before* this runs, so the frontend can poll
    for the reply and the placeholder is never without a run to measure liveness against.
    This function fills in the placeholder with the model output (``"completed"``) or marks
    it ``"failed"`` if the turn errors.

    The pooled connection is released before the model run starts: a turn can last minutes
    and there are only 30 connections for the whole application, so holding one across the
    run let a handful of concurrent turns starve every other request. Tools and contexts
    borrow their own short-lived sessions through ``AgentDeps.db()``.
    """
    # notify=False keeps the tutor's chat turns from spamming project members with
    # agent-run notifications while still recording tool usage as run logs.
    agent_logger = AgentLogger(agent_run_id, notify=False)
    usage = RunUsage()
    model_name = None

    try:
        with create_session() as session:
            conversation = get_or_create_conversation(session, project_id, user_id)

            # History excludes this turn's own user message and the assistant placeholder.
            prior_messages = replayable_messages(
                [
                    message
                    for message in conversation.messages
                    if message.id not in (user_message_id, assistant_message_id)
                ]
            )
            recent_messages = prior_messages[-MAX_HISTORY_MESSAGES:]
            message_history = _build_message_history(recent_messages)

            prompt = user_message
            if conversation.summary:
                prompt = (
                    "<contexto_resumido_da_conversa>\n"
                    f"{conversation.summary}\n"
                    "</contexto_resumido_da_conversa>\n\n"
                    f"{user_message}"
                )

            agent = create_agent_from_database(
                name=TUTOR_AGENT_NAME,
                project_id=project_id,
                session=session,
                output_type=str,
                agent_logger=agent_logger,
                optional_mcps=True,
            )
            model_name = str(agent.model)
            instructions = "\n\n".join(
                [TUTOR_OUTPUT_INSTRUCTIONS, tutor_sources_instructions(session, project_id)]
            )

        deps = AgentDeps(
            project_id=project_id,
            user_id=user_id,
            agent_logger=agent_logger,
            session_factory=create_session,
        )
        result = agent.run_sync(
            prompt,
            message_history=message_history,
            instructions=instructions,
            deps=deps,
            usage=usage,
            usage_limits=UsageLimits(tool_calls_limit=50),
        )
    except asyncio.CancelledError:
        logger.warning(
            "Tutor turn cancelled for project %s (dyno recycle or dropped connection)",
            project_id,
        )
        _write_assistant_result(assistant_message_id, FAILURE_MESSAGE, "failed")
        _finish_run(agent_run_id, model_name, usage, status="failed")
        raise
    except Exception:
        logger.exception("Tutor turn failed for project %s", project_id)
        _write_assistant_result(
            assistant_message_id, FAILURE_MESSAGE, "failed"
        )
        _finish_run(agent_run_id, model_name, usage, status="failed")
        return

    _write_assistant_result(assistant_message_id, result.output, "completed")
    _finish_run(
        agent_run_id,
        result.response.model_name,
        result.usage,
    )
    _update_rolling_summary(project_id, user_id)


def _update_rolling_summary(project_id: str, user_id: str) -> None:
    """Fold newly evicted messages into the conversation summary (best-effort).

    Only the messages that just fell out of the verbatim window are sent to the
    summarizer, together with the summary built so far. Re-summarizing the whole
    backlog every turn made cost grow with the square of the conversation length.
    """
    with create_session() as session:
        conversation = get_or_create_conversation(session, project_id, user_id)
        completed_messages = [
            message
            for message in conversation.messages
            if message.status == "completed"
        ]

        already_summarized = conversation.summarized_message_count or 0
        evicted_count = len(completed_messages) - MAX_HISTORY_MESSAGES
        if evicted_count <= already_summarized:
            return

        newly_evicted = completed_messages[already_summarized:evicted_count]
        summary = _summarize_older_messages(newly_evicted, conversation.summary)
        if summary:
            conversation.summary = summary
            conversation.summarized_message_count = evicted_count
            session.commit()
