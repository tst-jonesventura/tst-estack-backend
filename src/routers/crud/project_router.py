from typing import Annotated, List

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import aliased, selectinload

import schemas.user_story_schemas
from database import models
from database.database import SessionDep, create_session, delete_entity, update_entity
from database.models import User
from schemas import project_schemas, token_usage_schemas
from schemas.base import ProjectFullQueryParams, project_full_query_params
from services.agent_run_service import agent_run_service
from services.project_service import project_service
from utils.bearer_auth import (
    check_project_access,
    check_project_ownership,
    retrieve_admin_data,
    retrieve_user_data,
)
from utils.config import settings
from utils.token_usage import (
    build_ranked_items,
    model_label_column,
    specification_group_columns,
    token_sum_column,
)
from utils.validation import validate_entity_exists
from ...utils import log

router = APIRouter()
logger = log.setup_custom_logger(__name__)


# READ (project with user stories)
@router.get(
    "/{project_id}/full", response_model=project_schemas.ProjectWithFullStoriesPublic
)
def get_full_project(
        project_id: str,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
        query_params: Annotated[ProjectFullQueryParams, Depends(project_full_query_params)],
):
    logger.info(
        f"Listing full project for project_id: {project_id}, query_params: {query_params}"
    )

    project = validate_entity_exists(session, models.Project, project_id, "Project")

    # Get user stories for the project
    user_story_query = session.query(models.UserStory)

    if query_params.user_story_name:
        user_story_query = user_story_query.filter(
            models.UserStory.name.ilike(f"%{query_params.user_story_name}%")
        )

    if query_params.user_story_description:
        user_story_query = user_story_query.filter(
            models.UserStory.description.ilike(
                f"%{query_params.user_story_description}%"
            )
        )

    if query_params.user_story_project_kanban_columns:
        user_story_query = user_story_query.filter(
            models.UserStory.project_kanban_column_id.in_(
                query_params.user_story_project_kanban_columns
            )
        )

    user_stories = (
        user_story_query.filter(models.UserStory.project_id == project_id)
        .options(
            selectinload(models.UserStory.dev_steps),
            selectinload(models.UserStory.tags),
            selectinload(models.UserStory.user),
        )
        .order_by(models.UserStory.timestamp.desc())
        .all()
    )

    story_ids = [s.id for s in user_stories]
    last_event_by_story_id: dict[str, schemas.user_story_schemas.UserStoryLastEventPublic] = {}
    if story_ids:
        user_alias = aliased(models.User)
        latest_tx_by_story = (
            session.query(
                models.UserStoryVersion.id.label("id"),
                func.max(models.UserStoryVersion.transaction_id).label("max_tx"),
            )
            .filter(models.UserStoryVersion.id.in_(story_ids))
            .group_by(models.UserStoryVersion.id)
            .subquery()
        )

        last_versions_with_user_email = (
            session.query(models.UserStoryVersion, user_alias.email)
            .join(
                latest_tx_by_story,
                (models.UserStoryVersion.id == latest_tx_by_story.c.id)
                & (
                        models.UserStoryVersion.transaction_id
                        == latest_tx_by_story.c.max_tx
                ),
            )
            .outerjoin(user_alias, user_alias.id == models.UserStoryVersion.user_id)
            .all()
        )
        for version, email in last_versions_with_user_email:
            # `version.id` is the original entity id in continuum
            last_event_by_story_id[version.id] = (
                schemas.user_story_schemas.UserStoryLastEventPublic(
                    description=getattr(version, "log", None) or "Alteração",
                    actor_email=email,
                    occurred_at=version.timestamp,
                )
            )

    # For each user story, get its acceptance criteria and dev steps
    user_stories_with_criteria = []
    for story in user_stories:
        story_public = schemas.user_story_schemas.UserStoryFullPublic.model_validate(
            story
        )
        story_public.last_event = last_event_by_story_id.get(story.id)

        # Fallback for stories with no version rows yet
        if story_public.last_event is None:
            story_public.last_event = schemas.user_story_schemas.UserStoryLastEventPublic(
                description=getattr(story, "log", None) or "Alteração",
                actor_email=getattr(getattr(story, "user", None), "email", None),
                occurred_at=story.timestamp,
            )
        story_public.dev_steps.sort(key=lambda x: x.order)
        user_stories_with_criteria.append(story_public)
    project_public = project_schemas.ProjectWithFullStoriesPublic.model_validate(
        project
    )
    project_public.user_stories = user_stories_with_criteria
    return project_public


# CREATE
@router.post("/", response_model=project_schemas.ProjectPublic)
def create_project(
        project_in: project_schemas.ProjectCreate,
        session: SessionDep,
        background_tasks: BackgroundTasks,
        user_data: Annotated[models.User, Depends(retrieve_admin_data)],
):
    if not project_in.organization_id:
        org = (
            session.query(models.Organization)
            .where(models.Organization.is_active)
            .where(models.Organization.name == settings.default_organization_name)
            .one()
        )
        project_in.organization_id = org.id

    project = models.Project(name=project_in.name, description=project_in.description,
                             organization_id=project_in.organization_id)
    session.add(project)

    # add the user as owner of the project
    role = session.execute(
        select(models.Role).where(models.Role.name == "owner")
    ).scalar_one()
    user_project = models.UserProject(user_id=user_data.id, project=project, role=role)
    session.add(user_project)

    session.commit()
    background_tasks.add_task(create_kanban_columns_for_project, project.id)
    background_tasks.add_task(assign_estack_user, session, project.id)
    return project


# READ (list)
@router.get("/", response_model=List[project_schemas.ProjectWithCountsPublic])
def list_projects(
        session: SessionDep,
        user_data: Annotated[models.User, Depends(retrieve_user_data)],
):
    results = (
        session.query(models.Project)
        .where(models.Project.is_active)
        .where(
            models.Project.user_roles.any(models.UserProject.user_id == user_data.id)
        )
        .order_by(models.Project.name.desc())
        .all()
    )
    projects_with_counts = []
    for project in results:
        user_stories_count = (
            session.query(models.UserStory)
            .filter(models.UserStory.project_id == project.id)
            .count()
        )
        tech_specs_count = (
            session.query(models.DocumentTechSpec)
            .filter(models.DocumentTechSpec.project_id == project.id)
            .count()
        )
        project_public = project_schemas.ProjectWithCountsPublic(
            **project.__dict__,
            user_stories_count=user_stories_count,
            tech_specs_count=tech_specs_count,
        )
        projects_with_counts.append(project_public)
    return projects_with_counts


# READ (admin token usage)
@router.get(
    "/token-usage",
    response_model=List[project_schemas.ProjectTokenUsagePublic],
)
def list_project_token_usage(
        session: SessionDep,
        user_data: Annotated[models.User, Depends(retrieve_admin_data)],
):
    projects_owned_by_user = project_service.projects_owned_by_user(session, user_data.id)
    if not projects_owned_by_user:
        return []

    project_ids = [project.id for project in projects_owned_by_user]
    usage_rows = agent_run_service.get_usage_rows_filtered_by_projects_ids_grouped_by_project(session, project_ids)
    if not usage_rows:
        return []

    usage_by_project: dict[str, list[project_schemas.ProjectModelTokenUsagePublic]] = {}
    for row in usage_rows:
        if len(row) == 5:
            project_id, model, agent_id, agent_name, total_tokens = row
        else:
            project_id, model, total_tokens = row
            agent_id = None
            agent_name = None
        tokens = int(total_tokens or 0)
        if tokens <= 0:
            continue
        usage_by_project.setdefault(project_id, []).append(
            project_schemas.ProjectModelTokenUsagePublic(
                model=model,
                agent_id=agent_id,
                agent_name=agent_name,
                total_tokens=tokens,
            )
        )

    response: list[project_schemas.ProjectTokenUsagePublic] = []
    for project in projects_owned_by_user:
        models_usage = usage_by_project.get(project.id, [])
        if not models_usage:
            continue
        models_usage.sort(key=lambda item: item.total_tokens, reverse=True)
        response.append(
            project_schemas.ProjectTokenUsagePublic(
                id=project.id,
                name=project.name,
                models=models_usage,
            )
        )
    return response


@router.get(
    "/{project_id}/token-usage/detail",
    response_model=token_usage_schemas.ProjectTokenUsageDetailPublic,
)
def get_project_token_usage_detail(
        project_id: str,
        session: SessionDep,
        user_data: Annotated[models.User, Depends(retrieve_admin_data)],
        _: Annotated[models.User, Depends(check_project_access)],
):
    project = validate_entity_exists(session, models.Project, project_id, "Project")
    model_label = model_label_column()

    projects_owned_by_user = project_service.projects_owned_by_user(session, user_data.id)
    if not projects_owned_by_user:
        return []
    if project not in projects_owned_by_user:
        return []

    contributor_rows = (
        session.query(
            models.AgentRun.user_id,
            models.AgentRun.project_id,
            models.User.email,
            model_label.label("model"),
            models.AgentRun.agent_id.label("agent_id"),
            models.Agent.name.label("agent_name"),
            token_sum_column().label("total_tokens"),
        )
        .join(models.User, models.AgentRun.user_id == models.User.id)
        .outerjoin(models.Agent, models.AgentRun.agent_id == models.Agent.id)
        .filter(models.AgentRun.project_id == project_id)
        .group_by(
            models.AgentRun.user_id,
            models.AgentRun.project_id,
            models.User.email,
            model_label,
            models.AgentRun.agent_id,
            models.Agent.name,
        )
        .all()
    )

    spec_id, spec_label = specification_group_columns()
    specification_rows = (
        session.query(
            spec_id.label("spec_id"),
            models.AgentRun.project_id,
            spec_label.label("spec_label"),
            model_label.label("model"),
            models.AgentRun.agent_id.label("agent_id"),
            models.Agent.name.label("agent_name"),
            token_sum_column().label("total_tokens"),
        )
        .outerjoin(
            models.UserStory,
            models.AgentRun.user_story_id == models.UserStory.id,
        )
        .outerjoin(models.Agent, models.AgentRun.agent_id == models.Agent.id)
        .filter(models.AgentRun.project_id == project_id)
        .group_by(
            spec_id,
            models.AgentRun.project_id,
            spec_label,
            model_label,
            models.AgentRun.agent_id,
            models.Agent.name,
        )
        .all()
    )

    agent_rows = (
        session.query(
            models.AgentRun.agent_id.label("agent_id"),
            models.AgentRun.project_id,
            models.Agent.name.label("agent_name_label"),
            model_label.label("model"),
            models.AgentRun.agent_id.label("run_agent_id"),
            models.Agent.name.label("agent_name"),
            token_sum_column().label("total_tokens"),
        )
        .join(models.Agent, models.AgentRun.agent_id == models.Agent.id)
        .filter(models.AgentRun.project_id == project_id)
        .filter(models.AgentRun.agent_id.isnot(None))
        .group_by(
            models.AgentRun.agent_id,
            models.AgentRun.project_id,
            models.Agent.name,
            model_label,
        )
        .all()
    )

    contributors = build_ranked_items(
        contributor_rows,
        token_usage_schemas.ModelTokenUsagePublic,
        token_usage_schemas.RankedTokenUsageItemPublic,
    )
    specifications = build_ranked_items(
        specification_rows,
        token_usage_schemas.ModelTokenUsagePublic,
        token_usage_schemas.RankedTokenUsageItemPublic,
    )
    agents = build_ranked_items(
        agent_rows,
        token_usage_schemas.ModelTokenUsagePublic,
        token_usage_schemas.RankedTokenUsageItemPublic,
    )

    return token_usage_schemas.ProjectTokenUsageDetailPublic(
        id=project.id,
        name=project.name,
        contributors=contributors,
        specifications=specifications,
        agents=agents,
    )


# READ (single)
@router.get("/{project_id}", response_model=project_schemas.ProjectPublic)
def get_project(
        project_id: str,
        session: SessionDep,
        has_access: Annotated[models.User, Depends(check_project_access)],
):
    return validate_entity_exists(session, models.Project, project_id, "Project")


# UPDATE
@router.put("/{project_id}", response_model=project_schemas.ProjectPublic)
def update_project(
        project_id: str,
        project_in: project_schemas.ProjectUpdate,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    project = validate_entity_exists(session, models.Project, project_id, "Project")
    return update_entity(session, project, project_in.model_dump())


# DELETE
@router.delete("/{project_id}", response_model=dict)
def delete_project(
        project_id: str,
        session: SessionDep,
        is_owner: Annotated[bool, Depends(check_project_ownership)],
):
    project = validate_entity_exists(session, models.Project, project_id, "Project")
    return delete_entity(session, project)


def create_kanban_columns_for_project(project_id: str):
    default_columns = settings.default_kanban_columns.split(",")

    with create_session() as session:
        for order, column_name in enumerate(default_columns):
            column = models.ProjectKanbanColumn(
                name=column_name.strip(), project_id=project_id, position=order
            )
            session.add(column)
        session.commit()


def assign_estack_user(session: SessionDep, project_id: str):
    # add the estack user as contributor of the project
    role = session.execute(
        select(models.Role).where(models.Role.name == "contributor")
    ).scalar_one()
    if role is None:
        logger.error("Role with name 'contributo' not found")
        raise ValueError("Role with name 'contributo' not found")

    user = session.query(User).filter(User.email == "e-stack@elumini-it.com").first()
    if user is None:
        logger.error("User with email 'E-stack' not found")
        raise ValueError("User with email 'E-stack' not found")

    user_project = models.UserProject(user_id=str(user.id), project_id=str(project_id), role=role)

    session.add(user_project)
    session.commit()
