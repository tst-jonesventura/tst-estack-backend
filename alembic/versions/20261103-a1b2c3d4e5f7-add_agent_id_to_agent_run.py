"""add agent_id to agent_run

Revision ID: a1b2c3d4e5f7
Revises: f2d468048fe4
Create Date: 2026-11-03 14:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f7"
down_revision: str | Sequence[str] | None = "f2d468048fe4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "agent_run",
        sa.Column("agent_id", sa.String(length=36), nullable=True),
    )
    op.create_index("ix_agent_run_agent_id", "agent_run", ["agent_id"], unique=False)
    op.create_foreign_key(
        "fk_agent_run_agent_id",
        "agent_run",
        "agent",
        ["agent_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("fk_agent_run_agent_id", "agent_run", type_="foreignkey")
    op.drop_index("ix_agent_run_agent_id", table_name="agent_run")
    op.drop_column("agent_run", "agent_id")
