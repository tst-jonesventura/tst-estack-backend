from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

import utils.log as log
from agents.tutor_agent import (
    create_run,
    fail_stale_processing_messages,
    get_or_create_conversation,
    process_tutor_turn,
)
from database import models
from database.database import SessionDep
from database.models import User
from schemas.tutor_schemas import (
    TutorConversationPublic,
    TutorMessageInput,
    TutorMessagePublic,
)
from utils.bearer_auth import check_project_access

logger = log.setup_custom_logger(__name__)
router = APIRouter()


@router.get("/{project_id}")
def get_tutor_conversation(
    project_id: str,
    session: SessionDep,
    user_data: Annotated[User, Depends(check_project_access)],
) -> TutorConversationPublic:
    conversation = get_or_create_conversation(session, project_id, user_data.id)
    # Self-heal turns that never completed (dead background task, dyno recycle, lost reply)
    # so the frontend stops polling forever and surfaces a retriable error instead.
    if fail_stale_processing_messages(session, conversation.id):
        session.refresh(conversation)
    return TutorConversationPublic.model_validate(conversation)


@router.post(
    "/{project_id}",
    responses={
        409: {"description": "A turn for this conversation is still being processed"}
    },
)
def send_tutor_message(
    project_id: str,
    payload: TutorMessageInput,
    session: SessionDep,
    user_data: Annotated[User, Depends(check_project_access)],
    background_tasks: BackgroundTasks,
) -> TutorMessagePublic:
    """Persist the user message and an assistant placeholder, then run the turn in the
    background. The reply is generated asynchronously (a tutor turn can exceed Heroku's 30s
    request timeout); the frontend polls ``GET /tutor/{project_id}`` until the returned
    assistant message leaves the ``processing`` state.
    """
    conversation = get_or_create_conversation(session, project_id, user_data.id)
    # Reconcile any stuck turn first so a stale placeholder doesn't keep the input disabled.
    fail_stale_processing_messages(session, conversation.id)

    turn_in_flight = (
        session.query(models.TutorMessage)
        .filter(
            models.TutorMessage.conversation_id == conversation.id,
            models.TutorMessage.role == "assistant",
            models.TutorMessage.status == "processing",
        )
        .first()
    )
    if turn_in_flight:
        raise HTTPException(
            status_code=409,
            detail="O Tutor ainda está respondendo a pergunta anterior.",
        )

    user_message = models.TutorMessage(
        conversation_id=conversation.id,
        role="user",
        content=payload.message,
        status="completed",
    )
    session.add(user_message)
    session.commit()
    session.refresh(user_message)

    agent_run = create_run(session, project_id, user_data.id, payload.message)

    assistant_message = models.TutorMessage(
        conversation_id=conversation.id,
        role="assistant",
        content="",
        status="processing",
        agent_run_id=agent_run.id,
    )
    session.add(assistant_message)
    session.commit()
    session.refresh(assistant_message)

    background_tasks.add_task(
        process_tutor_turn,
        project_id,
        user_data.id,
        payload.message,
        user_message.id,
        assistant_message.id,
        agent_run.id,
    )
    return TutorMessagePublic.model_validate(assistant_message)


@router.delete("/{project_id}")
def reset_tutor_conversation(
    project_id: str,
    session: SessionDep,
    user_data: Annotated[User, Depends(check_project_access)],
) -> TutorConversationPublic:
    conversation = get_or_create_conversation(session, project_id, user_data.id)
    conversation.messages.clear()
    conversation.summary = None
    conversation.summarized_message_count = 0
    session.commit()
    session.refresh(conversation)
    return TutorConversationPublic.model_validate(conversation)
