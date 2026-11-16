from typing import List, Annotated

from fastapi import APIRouter, BackgroundTasks, Depends

from database.database import SessionDep, create_entity, delete_entity, update_entity
from database.models import Project, Role, User, UserProject
from schemas import user_schemas
from services.email_service import email_service
from services.notification_service import notification_service
from utils.bearer_auth import check_project_access
from utils.validation import validate_entity_exists

router = APIRouter()


# READ (list for a specific project, with nested user and role)
@router.get(
    "/",
    response_model=List[user_schemas.UserProjectWithUserAndRolePublic],
)
def list_user_projects_by_project(
        project_id: str,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    estack_user = session.query(User).filter(User.email == "e-stack@elumini-it.com").first()
    if estack_user is None:
        user_projects = (
            session.query(UserProject)
            .filter(UserProject.project_id == project_id)
            .order_by(UserProject.created_at.desc())
            .all()
        )
    else:
        user_projects = (
            session.query(UserProject)
            .filter(UserProject.project_id == project_id)
            .filter(UserProject.user_id != estack_user.id)
            .order_by(UserProject.created_at.desc())
            .all()
        )
    # Return with nested user and role
    return [
        user_schemas.UserProjectWithUserAndRolePublic(
            id=up.id,
            user_id=up.user_id,
            project_id=up.project_id,
            role_id=up.role_id,
            created_at=up.created_at,
            updated_at=up.updated_at,
            user=user_schemas.UserPublic(
                id=up.user.id,
                email=up.user.email,
                is_active=up.user.is_active,
                is_admin=up.user.is_admin,
                created_at=up.user.created_at,
            ),
            role=(
                user_schemas.RolePublic(
                    id=up.role.id,
                    name=up.role.name,
                    description=up.role.description,
                )
            ),
        )
        for up in user_projects
    ]


# CREATE
@router.post("/", response_model=user_schemas.UserProjectPublic)
def create_user_project(
        project_id: str,
        user_project_in: user_schemas.UserProjectCreate,
        session: SessionDep,
        background_tasks: BackgroundTasks,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    # Validate existence of referenced entities
    user_entity = validate_entity_exists(session, User, user_project_in.user_id, "User")
    project_entity = validate_entity_exists(
        session, Project, user_project_in.project_id, "Project"
    )
    validate_entity_exists(session, Role, user_project_in.role_id, "Role")
    user_project = UserProject(
        user_id=user_project_in.user_id,
        project_id=user_project_in.project_id,
        role_id=user_project_in.role_id,
    )

    entity = create_entity(session, user_project)

    # Create and broadcast in-app notification
    notification_service.notify_user(
        session=session,
        user_id=user_entity.id,
        message=f"You have been invited to the project {project_entity.name}",
        notification_type="PROJECT_INVITE",
        project_id=project_entity.id,
    )

    background_tasks.add_task(
        email_service.send_invite_notification,
        email=user_entity.email,
        project_name=project_entity.name,
    )

    return entity


# READ (single)
@router.get("/{user_project_id}", response_model=user_schemas.UserProjectPublic)
def get_user_project(
        project_id: str,
        user_project_id: str,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    return validate_entity_exists(session, UserProject, user_project_id, "UserProject")


# UPDATE
@router.put("/{user_project_id}", response_model=user_schemas.UserProjectPublic)
def update_user_project(
        project_id: str,
        user_project_id: str,
        user_project_in: user_schemas.UserProjectUpdate,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    user_project = validate_entity_exists(session, UserProject, user_project_id, "UserProject")
    return update_entity(
        session, user_project, user_project_in.model_dump(exclude_unset=True)
    )


# DELETE
@router.delete("/{user_project_id}", response_model=dict)
def delete_user_project(
        project_id: str,
        user_project_id: str,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    user_project = validate_entity_exists(session, UserProject, user_project_id, "UserProject")
    return delete_entity(session, user_project)


@router.get("/roles/", response_model=List[user_schemas.RolePublic])
def get_all_roles(
        project_id: str,
        session: SessionDep,
        has_access: Annotated[bool, Depends(check_project_access)],
):
    return session.query(Role).all()
