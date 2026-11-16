"""conectores que leem bancos na rede do cliente e a fila de operacoes deles

Revision ID: f2d468048fe4
Revises: 0c7a4ed5cfd5
Create Date: 2026-10-05 10:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2d468048fe4"
down_revision: str | Sequence[str] | None = "0c7a4ed5cfd5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW_NULLABLE = (
    ("host", sa.String(length=255)),
    ("port", sa.Integer()),
    ("username", sa.Unicode(length=255)),
    ("password", sa.String()),
)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "database_connector",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.Unicode(length=100), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_hint", sa.String(length=16), nullable=False),
        sa.Column("version", sa.String(length=40), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reported_databases", sa.Unicode(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name="fk_database_connector_organization_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "ix_database_connector_organization_id", "database_connector", ["organization_id"]
    )

    op.create_table(
        "database_connector_job",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("connector_id", sa.String(length=36), nullable=False),
        sa.Column("database_alias", sa.Unicode(length=100), nullable=False),
        sa.Column("operation", sa.String(length=50), nullable=False),
        sa.Column("arguments", sa.Unicode(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("result", sa.String(), nullable=True),
        sa.Column("error", sa.Unicode(length=2000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("picked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["connector_id"],
            ["database_connector.id"],
            name="fk_database_connector_job_connector_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_database_connector_job_connector_status",
        "database_connector_job",
        ["connector_id", "status"],
    )

    op.add_column(
        "project_database",
        sa.Column(
            "access_mode", sa.String(length=20), nullable=False, server_default="direct"
        ),
    )
    op.add_column(
        "project_database", sa.Column("connector_id", sa.String(length=36), nullable=True)
    )
    op.add_column(
        "project_database", sa.Column("connector_alias", sa.Unicode(length=100), nullable=True)
    )
    op.create_foreign_key(
        "fk_project_database_connector_id",
        "project_database",
        "database_connector",
        ["connector_id"],
        ["id"],
    )
    op.create_index(
        "ix_project_database_connector_id", "project_database", ["connector_id"]
    )
    for column, column_type in NOW_NULLABLE:
        op.alter_column(
            "project_database",
            column,
            existing_type=column_type,
            nullable=True,
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DELETE FROM project_database WHERE access_mode = 'connector'")
    for column, column_type in NOW_NULLABLE:
        op.alter_column(
            "project_database",
            column,
            existing_type=column_type,
            nullable=False,
        )
    op.drop_index("ix_project_database_connector_id", table_name="project_database")
    op.drop_constraint(
        "fk_project_database_connector_id", "project_database", type_="foreignkey"
    )
    op.drop_column("project_database", "connector_alias")
    op.drop_column("project_database", "connector_id")
    op.drop_column("project_database", "access_mode", mssql_drop_default=True)
    op.drop_index(
        "ix_database_connector_job_connector_status", table_name="database_connector_job"
    )
    op.drop_table("database_connector_job")
    op.drop_index("ix_database_connector_organization_id", table_name="database_connector")
    op.drop_table("database_connector")
