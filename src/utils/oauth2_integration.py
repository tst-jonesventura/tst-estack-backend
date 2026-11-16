"""
OAuth2 integration module for ASAP-AI.
Integrates JWT token authentication with the existing conversational email/code authentication system.
"""

from datetime import datetime, timedelta, timezone
from random import SystemRandom
from typing import Optional

import jwt
from sqlmodel import select

from database.database import create_session
from database.models import User, UserAccessCode, UserProject
from schemas.user_schemas import Token

from .config import settings
from services.email_service import email_service
from .log import setup_custom_logger

logger = setup_custom_logger(__name__)


class OAuth2EmailCodeAuthenticator:
    """
    OAuth2 authenticator that uses email and verification code instead of username/password.
    Integrates with the existing conversational authentication system.
    """

    def __init__(self):
        self.email_service = email_service
        self.algorithm = "HS256"
        self.access_code_expiry = timedelta(minutes=settings.access_code_expire_minutes)
        self.access_token_expiry = timedelta(
            minutes=settings.access_token_expire_minutes
        )

    def generate_access_code(self):
        return f"{SystemRandom().randint(1000, 9999)}"

    def store_and_send_code(self, email: str) -> None:
        code = self.generate_access_code()
        with create_session() as session:
            user_access_code = UserAccessCode(
                user_email=email,
                access_code=code,
                access_code_expiry=datetime.now(timezone.utc) + self.access_code_expiry,
            )
            session.add(user_access_code)
            session.commit()
        self.email_service.send_authentication_code(email, code)

    def verify_code(self, email: str, code: str) -> bool:
        with create_session() as session:
            # Select the latest access code for the user (by expiry)
            statement = (
                select(UserAccessCode)
                .where(
                    UserAccessCode.user_email == email,
                    UserAccessCode.access_code_expiry > datetime.now(timezone.utc),
                )
                .order_by(UserAccessCode.access_code_expiry.desc())
            )
            latest_access_code = session.execute(statement).scalars().first()
            if latest_access_code is None:
                return False
            return latest_access_code.access_code == code

    def authenticate_user_with_code(self, email: str, code: str) -> Optional[Token]:
        """
        Authenticate a user using email and verification code.

        Args:
            email: User's email address
            code: Verification code sent to the email

        Returns:
            User data if authentication successful, None otherwise
        """
        code_valid = self.verify_code(email, code)
        if not code_valid:
            return None

        user_data = self._get_user_by_email(email)
        if not user_data:
            return None

        token = self.create_access_token(email)
        return token

    def create_access_token(self, email: str) -> Token:
        """
        Create a JWT access token for the authenticated user.

        Args:
            email: User's email address

        Returns:
            JWT access token
        """
        to_encode = {"sub": email}

        expire = datetime.now(timezone.utc) + self.access_token_expiry

        to_encode["exp"] = str(int(expire.timestamp()))
        encoded_jwt = jwt.encode(
            to_encode, settings.secret_hash_key, algorithm=self.algorithm
        )
        return Token(access_token=encoded_jwt)

    def verify_token(self, token: str) -> Optional[User]:
        """
        Verify a JWT token and return user data.

        Args:
            token: JWT token to verify

        Returns:
            User data if token is valid, None otherwise
        """
        try:
            payload = jwt.decode(
                token, settings.secret_hash_key, algorithms=[self.algorithm]
            )
            email = payload.get("sub")
            expiration = payload.get("exp")
            if expiration is None or datetime.fromtimestamp(
                int(expiration), tz=timezone.utc
            ) < datetime.now(timezone.utc):
                return None
            if email is None:
                return None

            user_data = self._get_user_by_email(email)
            return user_data

        except Exception as e:
            logger.error(f"Error verifying token: {e}")
            return None

    def _get_user_by_email(self, email: str) -> Optional[User]:
        """

        Args:
            email: Email address to look up

        Returns:
            User data dictionary or None if not found
        """
        with create_session() as session:
            statement = select(User).where(User.email == email.lower())
            user = session.execute(statement).scalar_one_or_none()
            return user

    def user_has_project_access(
        self, user_id: str, project_id: str, check_ownership: bool
    ) -> bool:
        """
        Verify if the user associated with the token has access to the specified project.

        Args:
            token: JWT token
            project_id: Project ID to check access for

        Returns:
            True if access is granted, False otherwise
        """
        with create_session() as session:
            statement = (
                select(UserProject)
                .where(UserProject.user_id == user_id)
                .where(UserProject.project_id == project_id)
            )
            role = session.execute(statement).scalar_one_or_none()
            if not role:
                return False

            if check_ownership and role.role.name != "owner":
                return False
            return True


oauth2_authenticator = OAuth2EmailCodeAuthenticator()
