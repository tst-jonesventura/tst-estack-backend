"""Conversational OpenHands agent."""

import json
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agents import openhands_agent, openhands_goals
from agents.openhands_goals import OpenHandsGoal
from agents.utils.agent_deps import AgentDeps
from database import models
from database.database import create_session
from pydantic_ai import RunUsage
from schemas.openhands_schemas import OpenHandsRepository
from sqlalchemy.orm import Session
from utils import log

if TYPE_CHECKING:
    from openhands.sdk import LocalConversation
    from openhands.sdk.mcp.config import MCPServer

logger = log.setup_custom_logger(__name__)

_STREAM_LOCK = threading.Lock()
_STREAM_BUFFERS: dict[str, list[str]] = {}

PERSISTENCE_DIRNAME = "conversations"
AGENT_RUN_TITLE = "OpenHands"
CONVERSATION_STATUS_ACTIVE = "active"
CONVERSATION_STATUS_OPENING_PR = "opening_pr"
CONVERSATION_STATUS_PR_OPENED = "pr_opened"
CONVERSATION_STATUS_PR_FAILED = "pr_failed"

FAILURE_MESSAGE = (
    "Desculpe, não consegui processar sua mensagem agora. Tente novamente em instantes."
)
PR_METADATA_FAILURE_MESSAGE = (
    "O OpenHands finalizou a execução, mas não retornou os metadados estruturados do "
    "pull request. Tente finalizar novamente."
)


@dataclass(frozen=True)
class PullRequestMetadata:
    message: str
    status: str
    branch_name: str
    pull_request_url: str
    pull_request_number: int


# region persistence helpers
def create_conversation(
    session: Session,
    project_id: str,
    user_id: str,
    goal: str,
    name: str | None = None,
) -> models.OpenHandsConversation:
    from utils.environment import (
        get_target_project_root,
        is_local_workspace_enabled,
        resolve_safe_target_path,
    )

    db_repos = (
        session.query(models.ProjectRepository)
        .filter(models.ProjectRepository.project_id == project_id)
        .all()
    )
    local_repo = next(
        (
            r
            for r in db_repos
            if r.source_type == models.McpProviderType.LOCAL_FILESYSTEM.value
        ),
        None,
    )

    if local_repo is not None or (is_local_workspace_enabled() and not db_repos):
        if local_repo and local_repo.local_path:
            workspace = resolve_safe_target_path(local_repo.local_path)
        else:
            workspace = get_target_project_root()
        workspace.mkdir(parents=True, exist_ok=True)
    else:
        workspace = openhands_agent.project_workspace_dir(project_id)

    conversation = models.OpenHandsConversation(
        project_id=project_id,
        user_id=user_id,
        workspace_dir=str(workspace),
        goal=goal,
        name=name,
        status=CONVERSATION_STATUS_ACTIVE,
    )
    session.add(conversation)
    session.commit()
    session.refresh(conversation)
    return conversation


def has_turn_in_flight(session: Session, conversation_id: str) -> bool:
    return (
        session.query(models.OpenHandsMessage)
        .filter(
            models.OpenHandsMessage.conversation_id == conversation_id,
            models.OpenHandsMessage.role == "assistant",
            models.OpenHandsMessage.status == "processing",
        )
        .first()
        is not None
    )


def start_turn(
    session: Session,
    conversation: models.OpenHandsConversation,
    user_message: str | None,
    run_input: str,
) -> models.OpenHandsMessage:
    """Persist the user message (if any), the AgentRun and the assistant placeholder.

    Done synchronously by the router, before the background turn, so the frontend can
    poll the placeholder until it leaves ``processing``.
    """
    if user_message is not None:
        session.add(
            models.OpenHandsMessage(
                conversation_id=conversation.id,
                role="user",
                content=user_message,
                status="completed",
            )
        )
        session.commit()

    from agents.utils.agent_factory import get_agent_info

    agent_info = get_agent_info(
        session=session, agent_name="openhands_agent", project_id=conversation.project_id
    ) or get_agent_info(
        session=session, agent_name="openhands", project_id=conversation.project_id
    )
    agent_id = agent_info.id if agent_info else None

    agent_run = models.AgentRun(
        user_id=conversation.user_id,
        project_id=conversation.project_id,
        agent_id=agent_id,
        title=AGENT_RUN_TITLE,
        status="running",
        input=run_input,
        is_internal=True,
    )
    session.add(agent_run)
    session.commit()

    placeholder = models.OpenHandsMessage(
        conversation_id=conversation.id,
        role="assistant",
        content="",
        status="processing",
        agent_run_id=agent_run.id,
    )
    session.add(placeholder)
    session.commit()
    session.refresh(placeholder)
    return placeholder


def _finish_turn(
    assistant_message_id: str,
    content: str,
    status: str,
    model: str | None = None,
    usage: RunUsage | None = None,
) -> None:
    """Write the assistant reply and close its AgentRun.

    Uses its own short-lived session: the turn may last minutes and the session used
    to start it is long gone.
    """
    with create_session() as session:
        message = session.get(models.OpenHandsMessage, assistant_message_id)
        if message is None:
            return
        message.content = content
        message.status = status

        if message.agent_run_id:
            agent_run = session.get(models.AgentRun, message.agent_run_id)
            if agent_run:
                agent_run.status = "finished" if status == "completed" else "failed"
                agent_run.model = model
                if usage:
                    agent_run.requests = usage.requests
                    agent_run.tool_calls = usage.tool_calls
                    agent_run.input_tokens = usage.input_tokens
                    agent_run.output_tokens = usage.output_tokens
                    agent_run.cache_read_tokens = usage.cache_read_tokens
                    agent_run.cache_write_tokens = usage.cache_write_tokens

        session.commit()


def register_stream_buffer(message_id: str) -> None:
    with _STREAM_LOCK:
        _STREAM_BUFFERS[message_id] = []


def _persist_stream_text(message_id: str, text: str | None) -> None:
    """Mirror the buffer to the message row, which is what the SSE endpoint reads.

    Streaming is best-effort presentation: a failure here must never abort the turn.
    """
    try:
        with create_session() as session:
            session.query(models.OpenHandsMessage).filter_by(id=message_id).update(
                {"stream_content": text}
            )
            session.commit()
    except Exception:
        logger.exception("Failed to persist OpenHands stream buffer for %s", message_id)


def append_completed_thought(message_id: str, thought: str) -> None:
    """Persist one complete reasoning block for cross-worker SSE delivery."""
    thought = thought.strip()
    if not thought:
        return
    with _STREAM_LOCK:
        buffer = _STREAM_BUFFERS.setdefault(message_id, [])
        buffer.append(thought)
        snapshot = json.dumps(buffer)
    _persist_stream_text(message_id, snapshot)


def get_stream_buffer(message_id: str) -> list[str]:
    with _STREAM_LOCK:
        return list(_STREAM_BUFFERS.get(message_id, []))


def clear_stream_buffer(message_id: str) -> None:
    with _STREAM_LOCK:
        _STREAM_BUFFERS.pop(message_id, None)
    _persist_stream_text(message_id, None)


def extract_completed_thought(event: Any) -> str:
    """Return the display thought from a finalized SDK action event."""
    from openhands.sdk.event.llm_convertible import ActionEvent

    if isinstance(event, ActionEvent):
        thought = getattr(event, "thought", ())
        return " ".join(
            text
            for part in thought
            if isinstance((text := getattr(part, "text", None)), str) and text
        ).strip()
    return ""


# endregion


# region prompts
def build_context_message(
    repositories: list[OpenHandsRepository],
    prior_messages: list[models.OpenHandsMessage],
    goal: OpenHandsGoal | None = None,
) -> str:
    """Opening context for a fresh SDK session (first turn or lost persistence)."""
    text = (
        "You are pairing with a developer on a project made of the following GitHub "
        "repositories. Each one is already cloned and up to date with its default "
        "branch in the local directory indicated below (relative to your current "
        "working directory). Do not clone them again; use those clones to answer "
        "questions, investigate the code and agree on a plan with the developer. "
        "You also have GitHub MCP tools available:\n"
        f"{openhands_agent.describe_repositories(repositories)}\n"
        "Until the developer explicitly asks you to execute the task, do NOT modify "
        "files, commit, push or open pull requests."
        " Keep your answers concise.\n"
        "The filesystem contains only the repository, not the entire system, dependencies "
        "or secrets.\n Do not expect to be able to run the code successfully.\n"
    )
    if goal is not None:
        text = f"{goal.context_prompt}\n{text}"
    if prior_messages:
        transcript = "\n".join(
            f"{'Developer' if m.role == 'user' else 'You'}: {m.content}"
            for m in prior_messages
        )
        text += (
            "\nThis is the conversation so far (your previous session was lost):\n"
            f"{transcript}\n"
        )
    return text


def build_finish_message() -> str:
    return (
        "The developer clicked the button to finish this conversation and open a "
        "pull request. Treat this as authorization to publish only changes that are "
        "already explicitly agreed in the conversation or already implemented in the "
        "working tree. Do not treat this button press as a new instruction to design "
        "or implement work. Before committing, inspect the conversation transcript and "
        "the repository working tree. If there are no explicit agreed code changes, or "
        "if the conversation only contains investigation/planning/questions, do not "
        "edit files, commit, push, or open a pull request. In that case, your final "
        'response must be only this JSON shape: {"status": "pr_failed", '
        '"message": "No pull request was opened because there are no agreed '
        'code changes to publish.", "branch_name": null, '
        '"pull_request_url": null, "pull_request_number": null}. If agreed '
        "changes exist, commit them, push a branch whose name starts with "
        f"{openhands_agent.BRANCH_PREFIX}, and open exactly one pull request. "
        "Then your final response must be only a valid JSON object with these keys: "
        "status, message, branch_name, pull_request_url, pull_request_number. For a "
        "successful PR, status must be pr_opened and all PR metadata fields must be "
        "filled. Do not wrap JSON in Markdown and do not include any other text."
    )


def extract_pr_metadata(reply: str) -> PullRequestMetadata:
    try:
        payload = json.loads(reply)
    except json.JSONDecodeError as exc:
        raise ValueError("OpenHands final response is not valid JSON") from exc

    if not isinstance(payload, dict):
        raise TypeError("OpenHands final response must be a JSON object")
    status, message, branch_name, pull_request_url, pull_request_number = (
        extract_payload(payload)
    )
    if status == CONVERSATION_STATUS_PR_FAILED:
        message = _required_string(message, "failed PR response message")
        if any(
            value is not None
            for value in (branch_name, pull_request_url, pull_request_number)
        ):
            raise ValueError(
                "OpenHands failed PR response must not include PR metadata"
            )
        return PullRequestMetadata(
            message=message.strip(),
            status=CONVERSATION_STATUS_PR_FAILED,
            branch_name="",
            pull_request_url="",
            pull_request_number=0,
        )

    if status != CONVERSATION_STATUS_PR_OPENED:
        raise ValueError("OpenHands final response has an invalid PR status")
    message = _required_string(message, "message")
    branch_name = _required_string(branch_name, "branch name")
    pull_request_url = _required_string(pull_request_url, "pull request URL")
    if not isinstance(pull_request_number, int) or pull_request_number <= 0:
        raise ValueError("OpenHands final response has an invalid pull request number")
    if not branch_name.startswith(openhands_agent.BRANCH_PREFIX):
        raise ValueError("OpenHands final response has an invalid branch name")

    return PullRequestMetadata(
        message=message.strip(),
        status=CONVERSATION_STATUS_PR_OPENED,
        branch_name=branch_name.strip(),
        pull_request_url=pull_request_url.strip(),
        pull_request_number=pull_request_number,
    )


def extract_payload(payload):
    status = payload.get("status")
    message = payload.get("message")
    branch_name = payload.get("branch_name")
    pull_request_url = payload.get("pull_request_url")
    pull_request_number = payload.get("pull_request_number")

    return status, message, branch_name, pull_request_url, pull_request_number


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"OpenHands final response is missing the {field_name}")
    return value.strip()


# endregion


# region OpenHands session
def open_sdk_conversation(
    workspace_dir: str,
    openhands_conversation_id: str,
    mcp_config: dict[str, "MCPServer"],
    working_dir: str | None = None,
    callbacks: list[Callable[[Any], None]] | None = None,
) -> tuple["LocalConversation", bool]:
    """Resume the persisted SDK conversation, or create it. Returns (conv, resumed).

    ``workspace_dir`` holds the SDK persistence; ``working_dir`` (default: the same
    directory) is where the agent runs, i.e. the project workspace with the clones.
    """
    from openhands.sdk import Conversation

    workspace = Path(workspace_dir)
    workspace.mkdir(parents=True, exist_ok=True)
    persistence_dir = workspace / PERSISTENCE_DIRNAME
    conversation_id = uuid.UUID(openhands_conversation_id)
    resumed = (persistence_dir / conversation_id.hex / "base_state.json").exists()

    sdk_conversation = Conversation(
        agent=openhands_agent.build_agent(mcp_config),
        workspace=working_dir or str(workspace),
        persistence_dir=str(persistence_dir),
        conversation_id=conversation_id,
        callbacks=callbacks,
    )
    return sdk_conversation, resumed


def run_sdk_turn(
    sdk_conversation: "LocalConversation", message: str
) -> tuple[str, RunUsage]:
    """Send ``message``, run the agent and return (final reply, usage of this turn)."""
    from openhands.sdk.conversation.response_utils import get_agent_final_response
    from openhands.sdk.event import ActionEvent

    stats = sdk_conversation.conversation_stats
    metrics_before = stats.get_combined_metrics().deep_copy()
    events_before = len(sdk_conversation.state.events)

    try:
        sdk_conversation.send_message(message)
        sdk_conversation.run()
    finally:
        sdk_conversation.close()

    new_events = sdk_conversation.state.events[events_before:]
    metrics = stats.get_combined_metrics().diff(metrics_before)
    tokens = metrics.accumulated_token_usage
    usage = RunUsage(
        requests=len(metrics.token_usages),
        tool_calls=sum(1 for e in new_events if isinstance(e, ActionEvent)),
        input_tokens=tokens.prompt_tokens if tokens else 0,
        output_tokens=tokens.completion_tokens if tokens else 0,
        cache_read_tokens=tokens.cache_read_tokens if tokens else 0,
        cache_write_tokens=tokens.cache_write_tokens if tokens else 0,
    )
    return get_agent_final_response(new_events), usage


# endregion


@dataclass
class _TurnContext:
    is_local: bool
    repositories: list[OpenHandsRepository]
    workspace: str
    project_workspace: Path
    sdk_conversation_id: str
    prior_messages: list[models.OpenHandsMessage]
    github_token: str
    mcp_config: dict[str, "MCPServer"]
    goal: OpenHandsGoal | None


def _load_turn_context(
    conversation_id: str,
    assistant_message_id: str,
    user_message: str | None,
) -> _TurnContext:
    with create_session() as session:
        conversation = session.get(models.OpenHandsConversation, conversation_id)
        if conversation is None:
            raise ValueError(f"OpenHands conversation {conversation_id} not found")
        prior_messages = [
            m
            for m in conversation.messages
            if m.id != assistant_message_id
            and m.status == "completed"
            and m.content.strip()
        ]
        if user_message is not None and prior_messages:
            prior_messages = prior_messages[:-1]  # the current user message

        is_local, workspace, mcp_config, repos = (
            openhands_agent.resolve_project_workspace_and_scm(
                session, conversation.project_id
            )
        )
        token = ""
        if not is_local:
            try:
                deps = AgentDeps(
                    project_id=conversation.project_id,
                    user_id=conversation.user_id,
                    session=session,
                )
                token, _ = openhands_agent._resolve_github_access(deps)
            except Exception:
                token = ""

        return _TurnContext(
            is_local=is_local,
            repositories=repos
            or [
                OpenHandsRepository.model_validate(repo)
                for repo in conversation.project.repositories
            ],
            workspace=str(workspace) if is_local else conversation.workspace_dir,
            project_workspace=workspace
            if is_local
            else openhands_agent.project_workspace_dir(conversation.project_id),
            sdk_conversation_id=conversation.openhands_conversation_id,
            prior_messages=prior_messages,
            github_token=token,
            mcp_config=mcp_config,
            goal=openhands_goals.get_goal(conversation.goal),
        )


def _build_turn_message(
    ctx: _TurnContext, user_message: str | None, resumed: bool
) -> str:
    message = user_message or ""
    if not resumed:
        context = build_context_message(ctx.repositories, ctx.prior_messages, ctx.goal)
        message = f"{context}\n{message}"
    return message


def _execute_turn(
    conversation_id: str,
    assistant_message_id: str,
    user_message: str | None,
) -> tuple[str, str | None, RunUsage | None] | None:
    """Execute OpenHands SDK turn and return reply, model and usage or None on failure."""
    try:
        ctx = _load_turn_context(conversation_id, assistant_message_id, user_message)
        register_stream_buffer(assistant_message_id)

        def _on_event(event: Any) -> None:
            append_completed_thought(
                assistant_message_id, extract_completed_thought(event)
            )

        if not ctx.is_local and ctx.github_token:
            openhands_agent.sync_repositories(
                ctx.project_workspace, ctx.repositories, ctx.github_token
            )
        sdk_conversation, resumed = open_sdk_conversation(
            ctx.workspace,
            ctx.sdk_conversation_id,
            ctx.mcp_config,
            working_dir=str(ctx.project_workspace),
            callbacks=[_on_event],
        )
        message = _build_turn_message(ctx, user_message, resumed)
        reply, usage = run_sdk_turn(sdk_conversation, message)
        model = openhands_agent._model_name()
        return reply, model, usage
    except Exception:
        logger.exception("OpenHands turn failed for conversation %s", conversation_id)
        clear_stream_buffer(assistant_message_id)
        _finish_turn(
            assistant_message_id,
            FAILURE_MESSAGE,
            "failed",
        )
        return None


def _update_conversation_pr_status(
    conversation_id: str,
    status: str,
    branch_name: str | None = None,
    pull_request_url: str | None = None,
    pull_request_number: int | None = None,
) -> None:
    """Update conversation status and pull request metadata in database."""
    with create_session() as session:
        conversation = session.get(models.OpenHandsConversation, conversation_id)
        if conversation is not None:
            conversation.status = status
            if branch_name is not None:
                conversation.branch_name = branch_name
            if pull_request_url is not None:
                conversation.pull_request_url = pull_request_url
            if pull_request_number is not None:
                conversation.pull_request_number = pull_request_number
        session.commit()


def _handle_finish_turn(
    conversation_id: str,
    assistant_message_id: str,
    reply: str,
    model: str | None,
    usage: RunUsage | None,
) -> str | None:
    """Process PR metadata when finish_conversation is True. Returns message or None."""
    try:
        metadata = extract_pr_metadata(reply)
    except (TypeError, ValueError):
        logger.exception(
            "OpenHands finish turn returned invalid PR metadata for conversation %s",
            conversation_id,
        )
        _update_conversation_pr_status(conversation_id, CONVERSATION_STATUS_PR_FAILED)
        _finish_turn(assistant_message_id, PR_METADATA_FAILURE_MESSAGE, "failed")
        clear_stream_buffer(assistant_message_id)
        return None

    if metadata.status == CONVERSATION_STATUS_PR_FAILED:
        _update_conversation_pr_status(conversation_id, CONVERSATION_STATUS_PR_FAILED)
        _finish_turn(assistant_message_id, metadata.message, "failed", model, usage)
        clear_stream_buffer(assistant_message_id)
        return None

    _update_conversation_pr_status(
        conversation_id,
        CONVERSATION_STATUS_PR_OPENED,
        branch_name=metadata.branch_name,
        pull_request_url=metadata.pull_request_url,
        pull_request_number=metadata.pull_request_number,
    )
    return metadata.message


def process_turn(
    conversation_id: str,
    assistant_message_id: str,
    user_message: str | None = None,
    finish_conversation: bool = False,
) -> None:
    """Run one turn in the background and fill in the assistant placeholder.

    Forward a chat message to the persisted OpenHands session.
    """
    turn_result = _execute_turn(conversation_id, assistant_message_id, user_message)
    if turn_result is None:
        return

    reply, model, usage = turn_result

    if finish_conversation:
        finish_reply = _handle_finish_turn(
            conversation_id, assistant_message_id, reply, model, usage
        )
        if finish_reply is None:
            return
        reply = finish_reply

    _finish_turn(assistant_message_id, reply, "completed", model, usage)
    clear_stream_buffer(assistant_message_id)
