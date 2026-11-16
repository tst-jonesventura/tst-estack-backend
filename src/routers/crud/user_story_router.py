from datetime import datetime, timezone
from typing import Annotated, List

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import joinedload

import utils.log as log
from agents.labeler_agent import tag_and_commit_in_background
from agents.story_completion_agent import run_story_completion
from agents.utils.agent_run_utils import create_agent_logger
from database.database import SessionDep
from database.models import (
    GherkinRule,
    ProjectKanbanColumn,
    User,
    UserStory,
    UserStoryVersion,
)
from schemas import user_story_schemas
from services.kanban_service import kanban_service
from services.sync_service import SyncService
from utils.bearer_auth import check_project_access
from utils.validation import (
    validate_project_access,
    validate_user_story_in_project,
)

router = APIRouter()
logger = log.setup_custom_logger(__name__)
USER_STORY_NOT_FOUND_MSG = "User story not found"


# READ (single user story with everything)
@router.get("/{story_id}/full", response_model=user_story_schemas.UserStoryFullPublic)
def get_user_story_with_criteria(
        project_id: str,
        story_id: str,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    story = (
        session.query(UserStory)
        .filter(UserStory.id == story_id)
        .options(
            joinedload(UserStory.gherkin_rules).joinedload(GherkinRule.scenarios),
            joinedload(UserStory.tags),
            joinedload(UserStory.dev_steps),
        )
        .first()
    )
    if story is None:
        logger.error("User story not found for full retrieval")
        raise ValueError(USER_STORY_NOT_FOUND_MSG)
    story_public = user_story_schemas.UserStoryFullPublic.model_validate(story)
    return story_public


@router.get(
    "/{story_id}/versions",
    response_model=List[user_story_schemas.UserStoryVersionPublic],
)
def get_user_story_versions(
        project_id: str,
        story_id: str,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    story = session.get(UserStory, story_id)
    if story is None:
        logger.error("User story not found for version retrieval")
        raise ValueError(USER_STORY_NOT_FOUND_MSG)

    versions = (
        session.query(UserStoryVersion)
        .filter(UserStoryVersion.id == story_id)
        .order_by(UserStoryVersion.transaction_id.desc())  # type: ignore
        .all()
    )

    versions_public = []
    for version in versions:
        version_public = user_story_schemas.UserStoryVersionPublic.model_validate(
            version
        )
        versions_public.append(version_public)

    return versions_public


# CREATE
@router.post("/", response_model=user_story_schemas.UserStoryPublic)
def create_user_story(
        project_id: str,
        story_in: user_story_schemas.UserStoryCreate,
        session: SessionDep,
        background_tasks: BackgroundTasks,
        user_data: Annotated[User, Depends(check_project_access)],
):
    validate_project_access(session, project_id)

    kanban_column = (
        session.query(ProjectKanbanColumn)
        .filter(ProjectKanbanColumn.project_id == project_id)
        .order_by(ProjectKanbanColumn.position.asc())
        .first()
    )

    story = UserStory(
        name=story_in.name,
        description=story_in.description,
        points=story_in.points,
        is_done=story_in.is_done,
        project_id=project_id,
        log="História criada por usuário",
        timestamp=datetime.now(timezone.utc),
        user_id=user_data.id,
        project_kanban_column_id=kanban_column.id if kanban_column else None,
    )
    session.add(story)
    session.commit()
    session.refresh(story)

    sync_service = SyncService(session)
    background_tasks.add_task(sync_service.sync_create, project_id, story.id)

    agent_logger = create_agent_logger(
        session=session,
        user_id=user_data.id,
        project_id=project_id,
        agent_run_title=f"Criação de tags para user story {story.name}",
        user_story_id=story.id,
        agent_name="labeler",
    )
    background_tasks.add_task(
        tag_and_commit_in_background,
        project_id=project_id,
        user_id=user_data.id,
        input_id=story.id,
        input_type="user_story",
        agent_run_id=agent_logger.agent_run_id,
    )
    return story


# READ (list)
@router.get("/", response_model=List[user_story_schemas.UserStoryPublic])
def list_user_stories(
        project_id: str,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    results = session.query(UserStory).filter(UserStory.project_id == project_id).all()
    return results


# READ (single)
@router.get("/{story_id}", response_model=user_story_schemas.UserStoryFullPublic)
def get_user_story(
        project_id: str,
        story_id: str,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    story = (
        session.query(UserStory)
        .filter(UserStory.id == story_id)
        .options(
            joinedload(UserStory.gherkin_rules).joinedload(GherkinRule.scenarios),
            joinedload(UserStory.tags),
            joinedload(UserStory.dev_steps),
            joinedload(UserStory.kanban_column),
            joinedload(UserStory.branches),
        )
        .first()
    )
    if story is None:
        logger.error("User story not found for retrieval")
        raise ValueError(USER_STORY_NOT_FOUND_MSG)
    story_public = user_story_schemas.UserStoryFullPublic.model_validate(story)
    return story_public


# UPDATE
@router.put("/{story_id}", response_model=user_story_schemas.UserStoryPublic)
def update_user_story(
        project_id: str,
        story_id: str,
        story_in: user_story_schemas.UserStoryUpdate,
        session: SessionDep,
        background_tasks: BackgroundTasks,
        user_data: Annotated[User, Depends(check_project_access)],
):
    story = session.get(UserStory, story_id)
    if story is None:
        logger.error("User story not found for update")
        raise ValueError(USER_STORY_NOT_FOUND_MSG)

    was_done_before = story.is_done

    # Apply the update fields from story_in
    update_data = story_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(story, field, value)

    story.user_id = user_data.id
    story.log = "História atualizada por usuário"
    story.timestamp = datetime.now(timezone.utc)
    session.commit()

    sync_service = SyncService(session)
    background_tasks.add_task(sync_service.sync_update, project_id, story_id)

    agent_logger = create_agent_logger(
        session=session,
        user_id=user_data.id,
        project_id=project_id,
        agent_run_title=f"Criação de tags para user story {story.name}",
        user_story_id=story.id,
        agent_name="labeler",
    )
    session.commit()
    background_tasks.add_task(
        tag_and_commit_in_background,
        project_id=project_id,
        user_id=user_data.id,
        input_id=story.id,
        input_type="user_story",
        agent_run_id=agent_logger.agent_run_id,
    )

    if not was_done_before and story.is_done:
        background_tasks.add_task(
            run_story_completion,
            project_id,
            story.id,
            user_data.id,
        )

    return story


# Delete user story
@router.delete("/{story_id}", response_model=dict)
async def delete_user_story(
        project_id: str,
        story_id: str,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    story = session.get(UserStory, story_id)
    if story is None:
        logger.error("User story not found for deletion")
        raise ValueError(USER_STORY_NOT_FOUND_MSG)

    sync_service = SyncService(session)
    await sync_service.sync_delete(project_id, story_id)

    session.delete(story)
    session.commit()

    return {"detail": "User story deleted successfully"}


# Update user story kanban column (and potentially mark as done if it's the last column)
@router.put(
    "/{story_id}/kanban/{project_kanban_column_id}",
    response_model=user_story_schemas.UserStoryPublic,
)
def update_user_story_kanban(
        project_id: str,
        story_id: str,
        project_kanban_column_id: str,
        session: SessionDep,
        user_data: Annotated[User, Depends(check_project_access)],
        background_tasks: BackgroundTasks,
):
    story = (
        session.query(UserStory)
        .filter(UserStory.id == story_id)
        .options(joinedload(UserStory.gherkin_rules))
        .first()
    )
    if story is None:
        logger.error("User story not found for update")
        raise ValueError("User story not found")

    validate_user_story_in_project(project_id, story)
    story.user_id = user_data.id
    story.timestamp = datetime.now(timezone.utc)

    kanban_service.run_kanban_update_flow(
        session,
        background_tasks,
        project_id,
        story,
        project_kanban_column_id,
        user_data,
    )

    session.commit()
    return story
