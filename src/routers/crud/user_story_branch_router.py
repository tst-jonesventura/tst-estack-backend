from typing import Annotated, List
from fastapi import APIRouter, Depends, HTTPException, status
from ...database.database import SessionDep
from ...database.models import User, UserStoryBranch, UserStory
from ...schemas import user_story_schemas
from ...services.user_story_branch_service import user_story_branch_service
from ...utils.bearer_auth import check_project_access
from ...utils.validation import validate_entity_exists, validate_user_story_access

router = APIRouter()


@router.post("/", response_model=user_story_schemas.UserStoryBranchPublic, status_code=status.HTTP_201_CREATED)
def create_branch(
    project_id: str,
    branch_in: user_story_schemas.UserStoryBranchCreate,
    session: SessionDep,
    user_data: Annotated[User, Depends(check_project_access)],
):
    validate_user_story_access(session, project_id, branch_in.user_story_id)
    return user_story_branch_service.create_branch(session, branch_in)


@router.get("/{branch_id}", response_model=user_story_schemas.UserStoryBranchPublic)
def get_branch(
    project_id: str,
    branch_id: str,
    session: SessionDep,
    has_access: Annotated[bool, Depends(check_project_access)],
):
    branch = validate_entity_exists(session, UserStoryBranch, branch_id, "UserStoryBranch")
    validate_user_story_access(session, project_id, branch.user_story_id)
    return branch


@router.get("/user-story/{user_story_id}", response_model=List[user_story_schemas.UserStoryBranchPublic])
def list_branches_by_user_story(
    project_id: str,
    user_story_id: str,
    session: SessionDep,
    has_access: Annotated[bool, Depends(check_project_access)],
):
    validate_user_story_access(session, project_id, user_story_id)
    return user_story_branch_service.list_branches_by_user_story(session, user_story_id)


@router.delete("/{branch_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_branch(
    project_id: str,
    branch_id: str,
    session: SessionDep,
    has_access: Annotated[bool, Depends(check_project_access)],
):
    branch = validate_entity_exists(session, UserStoryBranch, branch_id, "UserStoryBranch")
    validate_user_story_access(session, project_id, branch.user_story_id)
    user_story_branch_service.delete_branch(session, branch_id)
