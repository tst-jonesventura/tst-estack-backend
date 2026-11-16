"""Idempotent seed for the GitHub MCP configuration of the default company.

Configures the platform's default company (``settings.default_organization_name``
/ ``DEFAULT_ORG``, e.g. ``Elumini-IT``) to use the GitHub MCP provider and stores
the GitHub App credentials currently used by the platform in an encrypted
per-provider configuration row.

The company must already exist (it owns the current projects and the GitHub App
installation). The GitHub credentials are read from the environment (the same
secrets already used by the app): ``GITHUB_APP_ID`` and
``GITHUB_APP_PRIVATE_KEY_BASE64``. Encryption requires
``ESTACK_CONFIG_ENCRYPTION_KEY`` to be set.

Safe to run multiple times.

Run from the ``src`` directory:

    cd src && uv run python -m scripts.seed_company_github_mcp
"""

from database import models
from database.database import create_session
from utils import log
from utils.config import settings

logger = log.setup_custom_logger(__name__)

MCP_PROVIDER = models.McpProviderType.GITHUB.value
DEFAULT_GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"


def seed() -> None:
    if not settings.config_encryption_key:
        raise RuntimeError(
            "ESTACK_CONFIG_ENCRYPTION_KEY is not set; cannot encrypt the GitHub "
            "MCP configuration. Set it before running this seed."
        )
    if settings.github_app_id is None or not settings.github_app_private_key_base64:
        raise RuntimeError(
            "GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY_BASE64 must be set to seed "
            "the GitHub MCP configuration."
        )

    company_name = settings.default_organization_name
    with create_session() as session:
        company = (
            session.query(models.Organization)
            .filter(models.Organization.name == company_name)
            .one_or_none()
        )
        if company is None:
            raise RuntimeError(
                f"Default company '{company_name}' not found. Ensure it exists "
                f"(DEFAULT_ORG) before seeding its GitHub MCP configuration."
            )

        company.mcp_provider = MCP_PROVIDER
        session.commit()
        session.refresh(company)
        logger.info(
            "Set mcp_provider='%s' for company '%s' (id=%s)",
            MCP_PROVIDER,
            company_name,
            company.id,
        )

        config = (
            session.query(models.OrganizationGithubMcpConfig)
            .filter(models.OrganizationGithubMcpConfig.organization_id == company.id)
            .one_or_none()
        )
        if config is None:
            config = models.OrganizationGithubMcpConfig(organization_id=company.id)
            session.add(config)
            logger.info("Creating GitHub MCP config for company '%s'", company_name)
        else:
            logger.info("Updating GitHub MCP config for company '%s'", company_name)

        config.github_app_id = str(settings.github_app_id)
        config.github_app_private_key_base64 = settings.github_app_private_key_base64
        config.mcp_url = DEFAULT_GITHUB_MCP_URL
        session.commit()
        session.refresh(config)

        print(
            f"Seeded GitHub MCP config for company '{company_name}' (id={company.id}) "
            f"provider={MCP_PROVIDER}, config id={config.id}"
        )


if __name__ == "__main__":
    seed()
