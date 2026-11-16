"""add openhands pr lifecycle

Revision ID: 9c1d8b4a6f20
Revises: 45abaf93dc34
Create Date: 2026-09-17 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9c1d8b4a6f20"
down_revision: str | Sequence[str] | None = "45abaf93dc34"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "openhands_conversation",
        sa.Column(
            "status", sa.String(length=20), server_default="active", nullable=False
        ),
    )
    op.add_column(
        "openhands_conversation",
        sa.Column("branch_name", sa.String(), nullable=True),
    )
    op.add_column(
        "openhands_conversation",
        sa.Column("pull_request_url", sa.String(), nullable=True),
    )
    op.add_column(
        "openhands_conversation",
        sa.Column("pull_request_number", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_openhands_conversation_project_user_status",
        "openhands_conversation",
        ["project_id", "user_id", "status"],
        unique=False,
    )

    op.add_column(
        "openhands_conversation",
        sa.Column("name", sa.String(length=100), nullable=True),
    )


def _drop_default_constraint(table_name: str, column_name: str) -> None:
    """Drop the auto-generated DEFAULT constraint on a column (required by MSSQL before dropping it)."""
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
    op.drop_index(
        "ix_openhands_conversation_project_user_status",
        table_name="openhands_conversation",
    )
    op.drop_column("openhands_conversation", "pull_request_number")
    op.drop_column("openhands_conversation", "pull_request_url")
    op.drop_column("openhands_conversation", "branch_name")
    _drop_default_constraint("openhands_conversation", "status")
    op.drop_column("openhands_conversation", "status")
    op.drop_column("openhands_conversation", "name")
