from typing import Annotated

import requests
from fastapi import APIRouter, HTTPException, Request, Response, Depends

import utils.log as log
from database.database import SessionDep
from database.models import OrganizationGitHubAppInstallation
from schemas.project_github_apps_schemas import (
    GitHubRepositoriesResponse,
    ProjectGitHubAppInstallationCreate,
    ProjectGitHubAppInstallationPublic,
)
from services.github_app_service import (
    GithubAppCredentialsNotFoundError,
    get_github_installation_by_id,
    get_project_github_repositories,
    load_github_app_credentials,
)
from utils.bearer_auth import retrieve_user_data

router = APIRouter()
logger = log.setup_custom_logger(__name__)


# WEBHOOK
@router.post("/webhook")
async def receive_webhook_event(request: Request, session: SessionDep):
    event = request.headers.get("X-GitHub-Event")
    payload = await request.json()
    action = payload.get("action")

    logger.info(f"Received GitHub webhook event: {event}, action: {action}")

    if event == "installation":
        installation_id = payload.get("installation", {}).get("id")
        if action == "deleted":
            db_installation = session.query(OrganizationGitHubAppInstallation).filter(
                OrganizationGitHubAppInstallation.github_app_installation_id == installation_id
            ).first()
            if db_installation:
                session.delete(db_installation)
                session.commit()
                logger.info(f"Deleted GitHub App Installation: {installation_id}")
        elif action == "created":
            # For 'created' via webhook, we might not have organization_id yet unless we use metadata or manual flow
            # The manual flow is in create_project_github_app_installation
            logger.info(f"GitHub App Installation created: {installation_id}")
    elif event == "installation_repositories":
        # Handle addition or removal of repositories if needed in the future
        logger.info(
            f"GitHub App Installation repositories changed for installation: {payload.get('installation', {}).get('id')}")

    return Response(status_code=200)


# CREATE
@router.post("/organization/{organization_id}/github_app_installations",
             response_model=ProjectGitHubAppInstallationPublic,
             responses={400: {"description": "GitHub App credentials not configured for the company"}})
def create_project_github_app_installation(
        organization_id: str,
        installation: ProjectGitHubAppInstallationCreate,
        session: SessionDep,
        user_data: Annotated[bool, Depends(retrieve_user_data)],
):
    db_installation = OrganizationGitHubAppInstallation(
        organization_id=organization_id,
        github_app_installation_id=installation.github_app_installation_id,
    )

    try:
        app_id, private_key_base64 = load_github_app_credentials(
            session, organization_id
        )
    except GithubAppCredentialsNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    github_app_installation = get_github_installation_by_id(
        installation.github_app_installation_id, app_id, private_key_base64
    )
    logger.info(f"GitHub App Installation: {github_app_installation}")

    if github_app_installation.get('account'):
        db_installation.github_account_login = github_app_installation.get('account').get('login')

    session.add(db_installation)
    session.commit()
    session.refresh(db_installation)
    return db_installation


# GET REPOSITORY LIST
@router.get("/organization/{organization_id}/github_app_installations/repositories",
            response_model=GitHubRepositoriesResponse,
            responses={400: {"description": "GitHub App credentials not configured for the company"},
                       404: {"description": "Project GitHub App Installation not found for the organization_id"},
                       500: {"description": "Error fetching repositories"}}
            )
def get_project_github_app_installation_repositories(
        organization_id: str,
        session: SessionDep,
        user_data: Annotated[bool, Depends(retrieve_user_data)],
):
    project_github_installation = session.query(OrganizationGitHubAppInstallation).filter(
        OrganizationGitHubAppInstallation.organization_id == organization_id).first()

    if not project_github_installation:
        raise HTTPException(status_code=404, detail="Project GitHub App Installation not found for the organization_id")

    try:
        app_id, private_key_base64 = load_github_app_credentials(
            session, organization_id
        )
    except GithubAppCredentialsNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        result = get_project_github_repositories(
            project_github_installation.github_app_installation_id,
            app_id,
            private_key_base64,
        )
        result["github_app_installation_id"] = project_github_installation.id
        return result
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            raise HTTPException(status_code=404, detail="GitHub App Installation not found") from e
        raise HTTPException(status_code=500, detail=f"Error fetching repositories: {str(e)}") from e
    except requests.RequestException as e:
        raise HTTPException(status_code=500, detail=f"Error fetching repositories: {str(e)}") from e
