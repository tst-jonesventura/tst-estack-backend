"""
Bearer token authentication for ASAP-AI.
Uses session IDs as bearer tokens.
"""

from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from database.database import create_session
from database.models import Project, User
from utils import log
from utils.oauth2_integration import oauth2_authenticator

logger = log.setup_custom_logger(__name__)
security = HTTPBearer()


def raise_forbidden(
    detail: str, email: str | None = None, project_id: str | None = None
):
    if email and project_id:
        logger.debug(f"User {email} does not have access to project {project_id}")
    elif email:
        logger.debug(f"User is not admin: {email}")
    else:
        logger.debug(detail)
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def ensure_admin(user_data):
    if not user_data.is_admin:
        raise_forbidden("Admin privileges required", email=user_data.email)


def ensure_project_access(user_data, project: Project, check_ownership=False):
    has_access = oauth2_authenticator.user_has_project_access(
        user_id=str(user_data.id),
        project_id=project.id,
        check_ownership=check_ownership,
    )
    if not has_access:
        if check_ownership:
            raise_forbidden(
                "User does not have ownership of the project",
                email=user_data.email,
                project_id=project.id,
            )
        else:
            raise_forbidden(
                "Access to the project is forbidden",
                email=user_data.email,
                project_id=project.id,
            )
    if not project.is_active:
        raise_forbidden(
            "Project is inactive",
            email=user_data.email,
            project_id=project.id,
        )


def get_validated_user_data(credentials: HTTPAuthorizationCredentials) -> User:
    """
    Helper to validate credentials and return user data, raising HTTPException if invalid.
    """
    if not credentials:
        logger.debug("No authorization credentials provided")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    jwt_token = credentials.credentials
    user_data = validate_jwt(jwt_token)
    if not user_data:
        logger.debug("No user data found in JWT token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token data",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user_data


def validate_jwt(jwt_token: str) -> Optional[User]:
    try:
        user_data = oauth2_authenticator.verify_token(jwt_token)
        if not user_data:
            logger.debug("Invalid or expired JWT token: %s", jwt_token)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired session",
                headers={"WWW-Authenticate": "Bearer"},
            )

        return user_data

    except Exception as e:
        logger.error("Error validating JWT token %s: %s", jwt_token, str(e))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not Authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


def retrieve_user_email(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> str:
    """
    Validate bearer token (JWT) and return email if valid.
    """
    user_data = get_validated_user_data(credentials)
    return str(user_data.email)


def retrieve_user_data(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> User:
    """
    Retrieve user information based on the provided bearer token.
    """
    return get_validated_user_data(credentials)


def check_project_ownership(
    project_id: str,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    user_data = get_validated_user_data(credentials)
    project = get_project_info(project_id)
    ensure_project_access(user_data, project, check_ownership=True)
    return user_data


def get_project_info(project_id: str) -> Project:
    with create_session() as session:
        project = session.query(Project).filter_by(id=project_id).scalar()
        return project


def check_project_access(
    project_id: str,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    user_data = get_validated_user_data(credentials)
    project = get_project_info(project_id)

    ensure_project_access(user_data, project, check_ownership=False)
    return user_data


def retrieve_admin_data(
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    user_data = get_validated_user_data(credentials)
    ensure_admin(user_data)
    return user_data
