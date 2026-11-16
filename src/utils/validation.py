from fastapi import HTTPException
from sqlalchemy.orm import Session

from database.models import (
    DocumentTranscription,
    Project,
    ProjectKanbanColumn,
    UserStory,
)


def validate_entity_exists(
    session: Session, entity_class, entity_id: str, entity_name: str
):
    """
    Generic validation that an entity exists.
    Returns the entity if valid, raises HTTPException(404) if not found.
    """
    entity = session.get(entity_class, entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail=f"{entity_name} not found")
    return entity


def validate_project_access(session: Session, project_id: str) -> Project:
    """
    Validates that a project exists and returns it.
    Raises HTTPException(404) if project not found.
    """
    return validate_entity_exists(session, Project, project_id, "Project")


def validate_user_story_in_project(project_id: str, user_story):
    """
    Validates that a user story belongs to the specified project.
    Raises HTTPException(404) if user story not found in project.
    """
    if not user_story:
        raise HTTPException(status_code=404, detail="UserStory not found")

    if user_story.project_id != project_id:
        raise HTTPException(
            status_code=404, detail="UserStory not found in this project"
        )

    return user_story


def validate_user_story_access(session: Session, project_id: str, user_story_id: str):
    """
    Validates that a user story exists and belongs to the specified project.
    Returns the user story if valid.
    """
    validate_project_access(session, project_id)
    user_story = session.get(UserStory, user_story_id)
    return validate_user_story_in_project(project_id, user_story)


def validate_document_transcription_access(
    session: Session, project_id: str, transcription_id: str
):
    """
    Validates that a document transcription exists and belongs to the specified project.
    Returns the transcription if valid.
    """
    validate_project_access(session, project_id)
    transcription = session.get(DocumentTranscription, transcription_id)
    if not transcription:
        raise HTTPException(status_code=404, detail="DocumentTranscription not found")

    if transcription.project_id != project_id:
        raise HTTPException(
            status_code=404, detail="DocumentTranscription not found in this project"
        )

    return transcription


def validate_project_kanban_column_in_project(
    session: Session, project_id: str, project_kanban_column_id: str
):
    """
    Validates that a kanban column belongs to the specified project.
    Raises HTTPException(404) if kanban column not found in project.
    """
    kanban_column = session.get(ProjectKanbanColumn, project_kanban_column_id)
    if not kanban_column:
        raise HTTPException(status_code=404, detail="ProjectKanbanColumn not found")

    if kanban_column.project_id != project_id:
        raise HTTPException(
            status_code=404, detail="ProjectKanbanColumn not found in this project"
        )

    return kanban_column
