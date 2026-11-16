"""Add local repository fields and support local_filesystem provider

Revision ID: a2f4c6e8d1b3
Revises: a1b2c3d4e5f7
Create Date: 2026-11-09 00:00:00.000000

"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a2f4c6e8d1b3"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "project_repository",
        sa.Column(
            "source_type",
            sa.String(length=50),
            server_default="github",
            nullable=False,
        ),
    )
    op.add_column(
        "project_repository",
        sa.Column("local_path", sa.String(), nullable=True),
    )
    op.alter_column(
        "project_repository",
        "organization_github_installation_id",
        existing_type=sa.String(length=36),
        nullable=True,
    )
    op.alter_column(
        "project_repository",
        "url",
        existing_type=sa.String(),
        nullable=True,
    )
    op.alter_column(
        "project_repository",
        "owner",
        existing_type=sa.String(),
        nullable=True,
    )
    op.alter_column(
        "project_repository",
        "language",
        existing_type=sa.String(),
        nullable=True,
    )
    op.alter_column(
        "project_repository",
        "full_name",
        existing_type=sa.String(),
        nullable=True,
    )
    op.alter_column(
        "project_repository",
        "default_branch",
        existing_type=sa.String(),
        nullable=True,
    )


def _drop_default_constraint(table_name: str, column_name: str) -> None:
    """Drop the auto-generated DEFAULT constraint on a column (required by MSSQL before dropping it)."""
    bind = op.get_bind()
    if bind and bind.dialect.name == "mssql":
        op.execute(
            f"""
            DECLARE @constraint_name NVARCHAR(200)
            SELECT @constraint_name = dc.name
            FROM sys.default_constraints dc
            JOIN sys.columns c
                ON c.object_id = dc.parent_object_id AND c.column_id = dc.parent_column_id
            WHERE dc.parent_object_id = OBJECT_ID('{table_name}')
                AND c.name = '{column_name}'
            IF @constraint_name IS NOT NULL
                EXEC('ALTER TABLE {table_name} DROP CONSTRAINT ' + @constraint_name)
            """
        )


def downgrade() -> None:
    """Downgrade schema."""
    _drop_default_constraint("project_repository", "source_type")
    op.drop_column("project_repository", "local_path")
    op.drop_column("project_repository", "source_type")
    op.alter_column(
        "project_repository",
        "organization_github_installation_id",
        existing_type=sa.String(length=36),
        nullable=False,
    )
    op.alter_column(
        "project_repository",
        "url",
        existing_type=sa.String(),
        nullable=False,
    )
    op.alter_column(
        "project_repository",
        "owner",
        existing_type=sa.String(),
        nullable=False,
    )
    op.alter_column(
        "project_repository",
        "language",
        existing_type=sa.String(),
        nullable=False,
    )
    op.alter_column(
        "project_repository",
        "full_name",
        existing_type=sa.String(),
        nullable=False,
    )
    op.alter_column(
        "project_repository",
        "default_branch",
        existing_type=sa.String(),
        nullable=False,
    )
