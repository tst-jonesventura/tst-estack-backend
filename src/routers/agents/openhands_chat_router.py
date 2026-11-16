import asyncio
import json
import time
from typing import Annotated

from agents.openhands_chat_agent import (
    CONVERSATION_STATUS_ACTIVE,
    CONVERSATION_STATUS_OPENING_PR,
    CONVERSATION_STATUS_PR_FAILED,
    CONVERSATION_STATUS_PR_OPENED,
    build_finish_message,
    create_conversation,
    has_turn_in_flight,
    process_turn,
    start_turn,
)
from agents.openhands_goals import GOALS
from database import models
from database.database import SessionDep, create_session
from database.models import User
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from schemas.openhands_schemas import (
    OpenHandsConversationPublic,
    OpenHandsConversationSummary,
    OpenHandsConversationUpdate,
    OpenHandsMessageInput,
    OpenHandsMessagePublic,
)
from utils import log
from utils.bearer_auth import check_project_access, retrieve_user_data

logger = log.setup_custom_logger(__name__)
router = APIRouter()

TURN_IN_FLIGHT_DETAIL = "O OpenHands ainda está processando a mensagem anterior."
CONVERSATION_CLOSED_DETAIL = "Esta conversa já foi finalizada."
CONVERSATION_FINISHING_DETAIL = "O OpenHands já está abrindo o pull request."
NO_REPOSITORIES_DETAIL = "O projeto não possui repositórios associados."
UNKNOWN_GOAL_DETAIL = "Objetivo do OpenHands desconhecido."
FINISH_USER_MESSAGE = "Finalizar conversa e abrir pull request."

STREAM_POLL_INTERVAL = 0.25
# Must stay under the frontend budget, and a turn can legitimately take many minutes.
STREAM_TIMEOUT = 20 * 60
# Idle proxies (the Heroku router drops at 55s) need bytes on the wire to keep the
# connection open while the agent runs a long tool.
STREAM_HEARTBEAT_INTERVAL = 10.0


def _get_conversation(
    session, conversation_id: str, user_data: User
) -> models.OpenHandsConversation:
    conversation = (
        session.query(models.OpenHandsConversation)
        .filter_by(id=conversation_id, user_id=user_data.id)
        .one_or_none()
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _ensure_idle(session, conversation: models.OpenHandsConversation) -> None:
    if has_turn_in_flight(session, conversation.id):
        raise HTTPException(status_code=409, detail=TURN_IN_FLIGHT_DETAIL)


def _ensure_active(conversation: models.OpenHandsConversation) -> None:
    if conversation.status == CONVERSATION_STATUS_OPENING_PR:
        raise HTTPException(status_code=409, detail=CONVERSATION_FINISHING_DETAIL)
    if conversation.status not in (
        CONVERSATION_STATUS_ACTIVE,
        CONVERSATION_STATUS_PR_FAILED,
    ):
        raise HTTPException(status_code=409, detail=CONVERSATION_CLOSED_DETAIL)


def _schedule_turn(
    background_tasks: BackgroundTasks,
    conversation: models.OpenHandsConversation,
    placeholder: models.OpenHandsMessage,
    user_message: str | None = None,
    finish_conversation: bool = False,
) -> None:
    background_tasks.add_task(
        process_turn,
        conversation_id=conversation.id,
        assistant_message_id=placeholder.id,
        user_message=user_message,
        finish_conversation=finish_conversation,
    )


@router.get("/projects/{project_id}")
def list_conversations(
    project_id: str,
    session: SessionDep,
    user_data: Annotated[User, Depends(check_project_access)],
    goal: str | None = None,
) -> list[OpenHandsConversationSummary]:
    query = session.query(models.OpenHandsConversation).filter_by(
        project_id=project_id, user_id=user_data.id
    )
    if goal is not None:
        query = query.filter_by(goal=goal)
    conversations = query.order_by(models.OpenHandsConversation.created_at.desc()).all()
    return [OpenHandsConversationSummary.model_validate(c) for c in conversations]


@router.post(
    "/projects/{project_id}/goals/{goal_id}",
    responses={
        404: {"description": "Project has no repositories, or unknown goal"},
    },
)
def create_openhands_conversation_with_goal(
    project_id: str,
    goal_id: str,
    payload: OpenHandsMessageInput,
    session: SessionDep,
    background_tasks: BackgroundTasks,
    user_data: Annotated[User, Depends(check_project_access)],
) -> OpenHandsConversationPublic:
    """Open an OpenHands session on the project repositories, scoped to a goal preset.

    The reply is generated in the background; poll ``GET /openhands/conversations/{id}``
    until the assistant message leaves ``processing``.
    """
    if goal_id not in GOALS:
        raise HTTPException(status_code=404, detail=UNKNOWN_GOAL_DETAIL)
    project = session.get(models.Project, project_id)
    if not project or not project.repositories:
        raise HTTPException(status_code=404, detail=NO_REPOSITORIES_DETAIL)

    conversation_name = " ".join(payload.message.split())[:100]
    conversation = create_conversation(
        session,
        project_id,
        user_data.id,
        goal_id,
        conversation_name,
    )
    placeholder = start_turn(session, conversation, payload.message, payload.message)
    _schedule_turn(background_tasks, conversation, placeholder, payload.message)
    session.refresh(conversation)
    return OpenHandsConversationPublic.model_validate(conversation)


@router.get("/{conversation_id}", responses={404: {"description": "Not found"}})
def get_openhands_conversation(
    conversation_id: str,
    session: SessionDep,
    user_data: Annotated[User, Depends(retrieve_user_data)],
) -> OpenHandsConversationPublic:
    conversation = _get_conversation(session, conversation_id, user_data)
    return OpenHandsConversationPublic.model_validate(conversation)


@router.put("/{conversation_id}", responses={404: {"description": "Not found"}})
def update_openhands_conversation(
    conversation_id: str,
    payload: OpenHandsConversationUpdate,
    session: SessionDep,
    user_data: Annotated[User, Depends(retrieve_user_data)],
) -> OpenHandsConversationSummary:
    conversation = _get_conversation(session, conversation_id, user_data)
    conversation.name = payload.name
    session.commit()
    session.refresh(conversation)
    return OpenHandsConversationSummary.model_validate(conversation)


def _parse_stream_cursor(query_params) -> int:
    try:
        cursor = int(query_params.get("cursor", "0") or "0")
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Invalid stream cursor")
    return max(0, cursor)


def _get_message_for_stream(
    session,
    conversation: models.OpenHandsConversation,
    assistant_message_id: str | None,
) -> models.OpenHandsMessage | None:
    if assistant_message_id is None:
        return (
            session.query(models.OpenHandsMessage)
            .filter_by(
                conversation_id=conversation.id, role="assistant", status="processing"
            )
            .order_by(models.OpenHandsMessage.created_at.desc())
            .first()
        )

    return (
        session.query(models.OpenHandsMessage)
        .filter_by(id=assistant_message_id, conversation_id=conversation.id)
        .one_or_none()
    )


def _get_stream_message(
    session,
    conversation: models.OpenHandsConversation,
    assistant_message_id: str | None,
) -> models.OpenHandsMessage:
    message = _get_message_for_stream(session, conversation, assistant_message_id)
    if message is not None:
        return message

    if assistant_message_id is not None:
        raise HTTPException(status_code=404, detail="Message not found")
    raise HTTPException(status_code=404, detail="No active OpenHands turn found")


def _load_stream_thoughts(stream_content: str | None) -> list[str]:
    try:
        thoughts = json.loads(stream_content or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid OpenHands thought stream") from exc

    if not isinstance(thoughts, list) or not all(
        isinstance(thought, str) for thought in thoughts
    ):
        raise ValueError("Invalid OpenHands thought stream")
    return thoughts


async def _stream_message_events(
    request: Request,
    message: models.OpenHandsMessage,
    cursor: int,
):
    last_emitted = cursor
    started = time.monotonic()
    last_write = started

    while True:
        if await request.is_disconnected():
            return

        with create_session() as db:
            current = db.get(models.OpenHandsMessage, message.id)
            if current is None:
                return
            thoughts = _load_stream_thoughts(current.stream_content)
            status = current.status
            content = current.content

        for thought in thoughts[last_emitted:]:
            last_emitted += 1
            last_write = time.monotonic()
            yield _sse({"type": "thought", "text": thought, "cursor": last_emitted})

        if status != "processing":
            yield _sse({"type": "done", "content": content})
            return

        now = time.monotonic()
        if now - started > STREAM_TIMEOUT:
            yield _sse({"type": "timeout"})
            return
        if now - last_write >= STREAM_HEARTBEAT_INTERVAL:
            last_write = now
            yield ": ping\n\n"

        await asyncio.sleep(STREAM_POLL_INTERVAL)


async def _guarded_stream_message_events(
    request: Request,
    message: models.OpenHandsMessage,
    cursor: int,
):
    try:
        async for chunk in _stream_message_events(request, message, cursor):
            yield chunk
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("OpenHands stream failed for message %s", message.id)
        yield _sse({"type": "error"})


@router.get(
    "/{conversation_id}/stream",
    responses={
        404: {"description": "Not found"},
        422: {"description": "Invalid stream cursor"},
    },
)
async def stream_openhands_conversation(
    conversation_id: str,
    request: Request,
    session: SessionDep,
    user_data: Annotated[User, Depends(retrieve_user_data)],
):
    conversation = _get_conversation(session, conversation_id, user_data)
    cursor = _parse_stream_cursor(request.query_params)
    message = _get_stream_message(
        session,
        conversation,
        request.query_params.get("assistant_message_id"),
    )
    return StreamingResponse(
        _guarded_stream_message_events(request, message, cursor),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/{conversation_id}/messages",
    responses={
        404: {"description": "Not found"},
        409: {"description": "A turn for this conversation is still being processed"},
    },
)
def send_openhands_message(
    conversation_id: str,
    payload: OpenHandsMessageInput,
    session: SessionDep,
    background_tasks: BackgroundTasks,
    user_data: Annotated[User, Depends(retrieve_user_data)],
) -> OpenHandsMessagePublic:
    conversation = _get_conversation(session, conversation_id, user_data)
    _ensure_active(conversation)
    _ensure_idle(session, conversation)

    placeholder = start_turn(session, conversation, payload.message, payload.message)
    _schedule_turn(background_tasks, conversation, placeholder, payload.message)
    return OpenHandsMessagePublic.model_validate(placeholder)


@router.post(
    "/{conversation_id}/finish",
    responses={
        404: {"description": "Not found"},
        409: {"description": "Conversation is busy or already finalized"},
    },
)
def finish_openhands_conversation(
    conversation_id: str,
    session: SessionDep,
    background_tasks: BackgroundTasks,
    user_data: Annotated[User, Depends(retrieve_user_data)],
) -> OpenHandsConversationPublic:
    conversation = _get_conversation(session, conversation_id, user_data)
    if conversation.status == CONVERSATION_STATUS_PR_OPENED:
        raise HTTPException(status_code=409, detail=CONVERSATION_CLOSED_DETAIL)
    if conversation.status == CONVERSATION_STATUS_OPENING_PR:
        raise HTTPException(status_code=409, detail=CONVERSATION_FINISHING_DETAIL)
    _ensure_idle(session, conversation)

    conversation.status = CONVERSATION_STATUS_OPENING_PR
    session.commit()
    session.refresh(conversation)

    finish_message = build_finish_message()
    placeholder = start_turn(
        session,
        conversation,
        FINISH_USER_MESSAGE,
        finish_message,
    )
    _schedule_turn(
        background_tasks,
        conversation,
        placeholder,
        finish_message,
        finish_conversation=True,
    )
    session.refresh(conversation)
    return OpenHandsConversationPublic.model_validate(conversation)
