import base64
import subprocess
import time
from urllib.parse import urlparse, urlunparse

import jwt
import requests
import utils.log as log
from database.models import (
    OrganizationGitHubAppInstallation,
    OrganizationGithubMcpConfig,
)
from sqlalchemy.orm import Session
from utils import log

logger = log.setup_custom_logger(__name__)


GITHUB_ACCEPT_HEADER = "application/vnd.github+json"
GITHUB_API_VERSION = "2026-03-10"
GITHUB_REQUEST_TIMEOUT = (5, 15)
GITHUB_REPOSITORY_PAGE_SIZE = 100
GITHUB_MAX_REPOSITORY_PAGES = 10


class GithubAppCredentialsNotFoundError(RuntimeError):
    """Raised when a company has no encrypted GitHub App credentials stored."""


def load_github_app_credentials(
    session: Session, organization_id: str
) -> tuple[int, str]:
    """Load and decrypt a company's GitHub App credentials from the database.

    Returns ``(app_id, private_key_base64)`` read from the encrypted
    ``organization_github_mcp_config`` table, never from environment settings.
    """
    config = (
        session.query(OrganizationGithubMcpConfig)
        .filter(OrganizationGithubMcpConfig.organization_id == organization_id)
        .one_or_none()
    )
    if config is None:
        logger.error(
            "GitHub App credentials not configured for organization id=%s",
            organization_id,
        )
        raise GithubAppCredentialsNotFoundError(
            "GitHub App credentials are not configured for this company."
        )
    return int(config.github_app_id), config.github_app_private_key_base64


def _decode_private_key(private_key_base64: str) -> str:
    decoded_bytes = base64.b64decode(private_key_base64)
    return decoded_bytes.decode("utf-8")


def build_app_jwt(app_id: int, private_key_base64: str) -> str:
    """Build a short-lived GitHub App JWT from explicit credentials."""
    payload = {
        # Issued at time
        "iat": int(time.time()),
        # JWT expiration time (10 minutes maximum)
        "exp": int(time.time()) + 300,
        # GitHub App's client ID
        "iss": str(app_id),
    }
    return jwt.encode(
        payload, _decode_private_key(private_key_base64), algorithm="RS256"
    )


def get_installation_access_token(
    app_id: int, private_key_base64: str, installation_id: int
) -> str:
    """Exchange a GitHub App JWT for an installation access token.

    Uses explicit per-company credentials rather than the global settings.
    """
    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
    headers = {
        "Accept": GITHUB_ACCEPT_HEADER,
        "Authorization": f"Bearer {build_app_jwt(app_id, private_key_base64)}",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }

    response = requests.post(url, headers=headers, timeout=GITHUB_REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()["token"]


def get_github_installation_by_id(
    installation_id: int, app_id: int, private_key_base64: str
):
    """Fetch GitHub App installation metadata using explicit App credentials."""
    url = f"https://api.github.com/app/installations/{installation_id}"
    headers = {
        "Accept": GITHUB_ACCEPT_HEADER,
        "Authorization": f"Bearer {build_app_jwt(app_id, private_key_base64)}",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }

    response = requests.get(url, headers=headers, timeout=GITHUB_REQUEST_TIMEOUT)
    return response.json()


def get_project_github_repositories(
    installation_id: int, app_id: int, private_key_base64: str
):
    """List a company's GitHub repositories using explicit App credentials."""
    token = get_installation_access_token(app_id, private_key_base64, installation_id)
    return list_repositories_with_token(token)


def list_repositories_with_token(token: str) -> dict:
    """List repositories reachable by an installation access token."""
    url = "https://api.github.com/installation/repositories"
    headers = {
        "Accept": GITHUB_ACCEPT_HEADER,
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
    response = requests.get(url, headers=headers, timeout=GITHUB_REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def list_accessible_repository_full_names(token: str) -> set[str]:
    """Every ``owner/name`` the installation can reach, lowercased.

    An installation only reaches repositories on the account it was installed on
    and explicitly granted. A project configured with a repository outside that
    set produces empty searches rather than errors, which reads as "the code does
    not mention this" instead of "I cannot see this repository".
    """
    url = "https://api.github.com/installation/repositories"
    headers = {
        "Accept": GITHUB_ACCEPT_HEADER,
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
    names: set[str] = set()
    for page in range(1, GITHUB_MAX_REPOSITORY_PAGES + 1):
        response = requests.get(
            url,
            headers=headers,
            params={"per_page": GITHUB_REPOSITORY_PAGE_SIZE, "page": page},
            timeout=GITHUB_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        batch = response.json().get("repositories", [])
        names.update(
            repo["full_name"].lower() for repo in batch if repo.get("full_name")
        )
        if len(batch) < GITHUB_REPOSITORY_PAGE_SIZE:
            break
    return names
