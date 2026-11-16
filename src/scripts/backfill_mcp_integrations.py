"""Idempotent backfill of per-domain MCP integrations from the legacy column.

Every company whose ``organization.mcp_provider`` is set gains the equivalent
``OrganizationMcpIntegration`` row in the source-code (``scm``) domain.

Run this after the ``e4f5a6b7c8d9`` migration. Safe to run multiple times.

Run from the ``src`` directory:

    cd src && uv run python -m scripts.backfill_mcp_integrations
"""

from database import models
from database.database import create_session
from utils import log

logger = log.setup_custom_logger(__name__)

LEGACY_DOMAIN = models.McpDomain.SCM.value


def backfill() -> int:
    with create_session() as session:
        companies = (
            session.query(models.Organization)
            .filter(models.Organization.mcp_provider.isnot(None))
            .all()
        )

        created = 0
        for company in companies:
            existing = (
                session.query(models.OrganizationMcpIntegration)
                .filter(
                    models.OrganizationMcpIntegration.organization_id == company.id,
                    models.OrganizationMcpIntegration.domain == LEGACY_DOMAIN,
                )
                .one_or_none()
            )
            if existing is not None:
                logger.info(
                    "Company '%s' (id=%s) already has an '%s' integration; skipping.",
                    company.name,
                    company.id,
                    LEGACY_DOMAIN,
                )
                continue

            session.add(
                models.OrganizationMcpIntegration(
                    organization_id=company.id,
                    domain=LEGACY_DOMAIN,
                    provider=company.mcp_provider,
                    is_active=True,
                )
            )
            created += 1
            logger.info(
                "Created '%s' integration (provider='%s') for company '%s' (id=%s)",
                LEGACY_DOMAIN,
                company.mcp_provider,
                company.name,
                company.id,
            )

        session.commit()
        print(
            f"Backfill complete: {created} MCP integration(s) created from "
            f"{len(companies)} company/companies with a legacy provider."
        )
        return created


if __name__ == "__main__":
    backfill()
