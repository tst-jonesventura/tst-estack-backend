"""bancos de dados que os agentes de um projeto podem ler

Revision ID: 0c7a4ed5cfd5
Revises: 9c1d8b4a6f20
Create Date: 2026-09-28 10:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0c7a4ed5cfd5"
down_revision: str | Sequence[str] | None = "9c1d8b4a6f20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "project_database",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("alias", sa.Unicode(length=100), nullable=False),
        sa.Column("engine", sa.String(length=20), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("database_name", sa.Unicode(length=255), nullable=False),
        sa.Column("username", sa.Unicode(length=255), nullable=False),
        sa.Column("password", sa.String(), nullable=False),
        sa.Column("ssl_mode", sa.String(length=20), nullable=False),
        sa.Column("schema_allowlist", sa.Unicode(length=1000), nullable=True),
        sa.Column("description", sa.Unicode(length=1000), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name="fk_project_database_project_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "alias", name="uq_project_database_project_id_alias"
        ),
    )
    op.create_index(
        "ix_project_database_project_id", "project_database", ["project_id"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_project_database_project_id", table_name="project_database")
    op.drop_table("project_database")
