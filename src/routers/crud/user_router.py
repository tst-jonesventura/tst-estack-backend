from typing import Annotated, List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select

from database import models
from database.database import SessionDep, create_entity, delete_entity, update_entity
from database.models import User
from schemas import token_usage_schemas
from schemas import user_schemas
from schemas.user_schemas import Token
from services.agent_run_service import agent_run_service
from services.project_service import project_service
from utils.bearer_auth import retrieve_admin_data, retrieve_user_data
from utils.config import settings
from utils.log import setup_custom_logger
from utils.oauth2_integration import oauth2_authenticator
from utils.token_usage import (
    build_ranked_items,
    model_label_column,
    specification_group_columns,
    token_sum_column,
)
from utils.validation import validate_entity_exists

logger = setup_custom_logger(__name__)

router = APIRouter()


# CREATE
@router.post("/", response_model=user_schemas.UserPublic)
def create_user(
        user_in: user_schemas.UserCreate,
        session: SessionDep,
        admin_data: Annotated[User, Depends(retrieve_admin_data)],
):
    user = User(
        email=user_in.email, is_active=user_in.is_active, is_admin=user_in.is_admin
    )
    return create_entity(session, user)


@router.get("/me", response_model=user_schemas.UserPublic)
def get_current_user(
        session: SessionDep, user_data: Annotated[User, Depends(retrieve_user_data)]
):
    return validate_entity_exists(session, User, user_data.id, "User")


# READ (list)
@router.get("/", response_model=List[user_schemas.UserPublic])
def list_users(
        session: SessionDep, user_data: Annotated[User, Depends(retrieve_user_data)]
):
    results = session.query(User).order_by(User.id.asc()).all()
    return results


# READ (admin token usage)
@router.get(
    "/token-usage",
    response_model=List[user_schemas.UserTokenUsagePublic],
)
def list_user_token_usage(
        session: SessionDep,
        admin_data: Annotated[User, Depends(retrieve_admin_data)],
):
    projects_owned_by_user = project_service.projects_owned_by_user(session, admin_data.id)
    if not projects_owned_by_user:
        return []

    project_ids = [project.id for project in projects_owned_by_user]
    usage_rows = agent_run_service.get_usage_rows_filtered_by_projects_ids_grouped_by_user(session, project_ids)

    usage_by_user: dict[str, list[user_schemas.UserModelTokenUsagePublic]] = {}
    for row in usage_rows:
        if len(row) == 5:
            user_id, model, agent_id, agent_name, total_tokens = row
        else:
            user_id, model, total_tokens = row
            agent_id = None
            agent_name = None
        tokens = int(total_tokens or 0)
        if tokens <= 0:
            continue
        usage_by_user.setdefault(user_id, []).append(
            user_schemas.UserModelTokenUsagePublic(
                model=model,
                agent_id=agent_id,
                agent_name=agent_name,
                total_tokens=tokens,
            )
        )

    response: list[user_schemas.UserTokenUsagePublic] = []
    users = (session.query(User)
             .where(User.id.in_(usage_by_user.keys()))
             .order_by(User.email.asc()).all())
    if not users:
        return []
    for user in users:
        models_usage = usage_by_user.get(user.id, [])
        if not models_usage:
            continue
        models_usage.sort(key=lambda item: item.total_tokens, reverse=True)
        response.append(
            user_schemas.UserTokenUsagePublic(
                id=user.id,
                email=user.email,
                models=models_usage,
            )
        )
    return response


@router.get(
    "/{user_id}/token-usage/detail",
    response_model=token_usage_schemas.UserTokenUsageDetailPublic,
)
def get_user_token_usage_detail(
        user_id: str,
        session: SessionDep,
        admin_data: Annotated[User, Depends(retrieve_admin_data)],
):
    user = validate_entity_exists(session, User, user_id, "User")
    projects_owned_by_user = project_service.projects_owned_by_user(session, admin_data.id)
    if not projects_owned_by_user:
        return token_usage_schemas.UserTokenUsageDetailPublic(
            id=user.id,
            email=user.email,
            projects=[],
            specifications=[],
            agents=[],
        )

    project_ids = [project.id for project in projects_owned_by_user]
    model_label = model_label_column()

    project_rows = (
        session.query(
            models.AgentRun.project_id,
            models.AgentRun.project_id.label("actual_project_id"),
            models.Project.name,
            model_label.label("model"),
            models.AgentRun.agent_id.label("agent_id"),
            models.Agent.name.label("agent_name"),
            token_sum_column().label("total_tokens"),
        )
        .join(models.Project, models.AgentRun.project_id == models.Project.id)
        .outerjoin(models.Agent, models.AgentRun.agent_id == models.Agent.id)
        .filter(models.AgentRun.user_id == user_id)
        .filter(models.Project.id.in_(project_ids))
        .group_by(
            models.AgentRun.project_id,
            models.Project.name,
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
        .join(models.Project, models.AgentRun.project_id == models.Project.id)
        .outerjoin(
            models.UserStory,
            models.AgentRun.user_story_id == models.UserStory.id,
        )
        .outerjoin(models.Agent, models.AgentRun.agent_id == models.Agent.id)
        .filter(models.AgentRun.user_id == user_id)
        .filter(models.Project.id.in_(project_ids))
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
        .join(models.Project, models.AgentRun.project_id == models.Project.id)
        .join(models.Agent, models.AgentRun.agent_id == models.Agent.id)
        .filter(models.AgentRun.user_id == user_id)
        .filter(models.Project.id.in_(project_ids))
        .filter(models.AgentRun.agent_id.isnot(None))
        .group_by(
            models.AgentRun.agent_id,
            models.AgentRun.project_id,
            models.Agent.name,
            model_label,
        )
        .all()
    )

    projects = build_ranked_items(
        project_rows,
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

    return token_usage_schemas.UserTokenUsageDetailPublic(
        id=user.id,
        email=user.email,
        projects=projects,
        specifications=specifications,
        agents=agents,
    )


# READ (single)
@router.get("/{id}", response_model=user_schemas.UserPublic)
def get_user(
        id: str,
        session: SessionDep,
        admin_data: Annotated[User, Depends(retrieve_admin_data)],
):
    return validate_entity_exists(session, User, id, "User")


# UPDATE
@router.put("/{id}", response_model=user_schemas.UserPublic)
def update_user(
        id: str,
        user_in: user_schemas.UserUpdate,
        session: SessionDep,
        admin_data: Annotated[User, Depends(retrieve_admin_data)],
):
    user = validate_entity_exists(session, User, id, "User")
    return update_entity(session, user, user_in.model_dump())


# DELETE
@router.delete("/{id}", response_model=dict)
def delete_user(
        id: str,
        session: SessionDep,
        admin_data: Annotated[User, Depends(retrieve_admin_data)],
):
    user = validate_entity_exists(session, User, id, "User")
    return delete_entity(session, user)


@router.post(
    "/generate-access-code", responses={404: {"description": "User not found"}}
)
def generate_access_code(
        user_code: user_schemas.UserEmail,
        session: SessionDep,
        background_tasks: BackgroundTasks,
):
    stmt = select(User).where(User.email == user_code.email)
    user = session.execute(stmt).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    background_tasks.add_task(
        oauth2_authenticator.store_and_send_code, email=user_code.email
    )
    return 200


@router.post(
    "/validate-access-code",
    response_model=Token,
    responses={401: {"description": "Invalid or expired access code"}},
)
async def validate_access_code(user_code: user_schemas.UserCode, session: SessionDep):
    token = oauth2_authenticator.authenticate_user_with_code(
        user_code.email, user_code.code
    )
    if token is None:
        raise HTTPException(status_code=401, detail="Invalid or expired access code")

    return token


@router.post(
    "/local-login",
    response_model=Token,
    responses={
        403: {"description": "Local login is disabled in this environment"},
        404: {"description": "Admin user not found"},
    },
)
def local_login(session: SessionDep):
    """
    Direct one-click login endpoint for local Docker development.
    Strictly fail-safe: only enabled when ESTACK_ENVIRONMENT == 'local' and ESTACK_AUTH_BYPASS_LOCAL is True.
    """
    if not settings.is_local_dev_auth_enabled():
        raise HTTPException(
            status_code=403, detail="Local login is disabled in this environment"
        )

    admin_email = "admin@elumini-it.com"

    stmt = select(User).where(User.email == admin_email)
    user = session.execute(stmt).scalar_one_or_none()

    if not user:
        # Fallback to seed_admin_email or email_user or any admin user in DB
        fallback_email = (settings.seed_admin_email or "").strip() or (settings.email_user or "").strip()
        if fallback_email:
            user = session.execute(select(User).where(User.email == fallback_email)).scalar_one_or_none()

    if not user:
        stmt_admin = select(User).where(User.is_admin == True)  # noqa: E712
        user = session.execute(stmt_admin).scalars().first()

    if not user:
        raise HTTPException(
            status_code=404,
            detail=f"Default admin user not found for local login (searched email: '{admin_email}')",
        )

    logger.warning(
        "Local dev login triggered for user: %s (ESTACK_ENVIRONMENT=%s). JWT token issued.",
        user.email,
        settings.estack_environment,
    )

    token = oauth2_authenticator.create_access_token(user.email)
    return token
