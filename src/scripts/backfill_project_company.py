"""Idempotent backfill associating every existing project to a company.

Any project whose ``organization_id`` is still ``NULL`` is attached to the
platform's default company (``settings.default_organization_name`` /
``DEFAULT_ORG``). The company must already exist.

Safe to run multiple times.

Run from the ``src`` directory:

    cd src && uv run python -m scripts.backfill_project_company
"""

from database import models
from database.database import create_session
from utils import log
from utils.config import settings

logger = log.setup_custom_logger(__name__)


def backfill(company_name: str | None = None) -> int:
    company_name = company_name or settings.default_organization_name
    with create_session() as session:
        company = (
            session.query(models.Organization)
            .filter(models.Organization.name == company_name)
            .one_or_none()
        )
        if company is None:
            raise RuntimeError(
                f"Company '{company_name}' not found. Run "
                f"scripts.seed_company_github_mcp before backfilling."
            )

        orphans = (
            session.query(models.Project)
            .filter(models.Project.organization_id.is_(None))
            .all()
        )
        for project in orphans:
            project.organization_id = company.id

        session.commit()
        logger.info(
            "Associated %d project(s) to company '%s' (id=%s)",
            len(orphans),
            company_name,
            company.id,
        )
        print(
            f"Backfill complete: {len(orphans)} project(s) associated to "
            f"company '{company_name}' (id={company.id})"
        )
        return len(orphans)


if __name__ == "__main__":
    backfill()
