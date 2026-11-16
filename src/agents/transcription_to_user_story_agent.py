from datetime import datetime, timezone
from typing import List
from uuid import uuid4

import utils.log as log
from agents.utils.agent_deps import AgentDeps
from agents.utils.agent_factory import create_agent_from_database
from database.models import ProjectKanbanColumn, UserStory
from schemas.llm_output_schemas import TranscriptionToStoryOutput

logger = log.setup_custom_logger(__name__)


def run(
    transcription_formatted: str,
    deps: AgentDeps,
) -> List[UserStory]:
    if not deps.input_id:
        raise ValueError("Input ID is required in deps")
    transcription_to_story_agent = create_agent_from_database(
        name="transcription_to_story",
        project_id=deps.project_id,
        session=deps.session,
        agent_logger=deps.agent_logger
    )
    if not deps.agent_logger:
        logger.warning("No agent logger found in deps")
    else:
        deps.agent_logger.log("Transformando transcrição em user stories...")

    result = transcription_to_story_agent.run_sync(
        transcription_formatted,
        output_type=TranscriptionToStoryOutput,
        deps=deps,
    )
    deps.agent_logger.finish_agent_run_with_usage("Execução finalizada.", result.response.model_name, result.usage)

    new_user_stories = result.output.created
    updated_user_stories = result.output.updated
    user_story_db = []
    kanban_column = (
        deps.session.query(ProjectKanbanColumn)
        .filter(ProjectKanbanColumn.project_id == deps.project_id)
        .order_by(ProjectKanbanColumn.position.asc())
        .first()
    )

    for user_story in new_user_stories:
        user_story_db.append(
            UserStory(
                **user_story.model_dump(),
                project_id=deps.project_id,
                user_id=deps.user_id,
                project_kanban_column_id=kanban_column.id if kanban_column else None,
                transcription_id=deps.input_id,
                log="História criada por transcrição",
                id=str(uuid4()),
            )
        )
    for updated_user_story in updated_user_stories:
        user_story = (
            deps.session.query(UserStory)
            .filter_by(id=updated_user_story.id, project_id=deps.project_id)
            .one_or_none()
        )
        if not user_story:
            continue
        user_story.name = updated_user_story.name
        user_story.description = updated_user_story.description
        user_story.points = updated_user_story.points
        user_story.is_done = updated_user_story.is_done
        user_story.user_id = deps.user_id
        user_story.transcription_id = deps.input_id
        user_story.log = "História atualizada por transcrição"
        user_story.timestamp = datetime.now(timezone.utc)
        user_story_db.append(user_story)

    return user_story_db
