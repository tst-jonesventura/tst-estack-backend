"""tabela de integracoes mcp por dominio de conhecimento da empresa

A coluna organization.mcp_provider e mantida nesta revisao; o backfill fica no
script dedicado scripts.backfill_mcp_integrations, que roda apos esta migration.

Revision ID: e4f5a6b7c8d9
Revises: db642c952bcf
Create Date: 2026-08-07 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e4f5a6b7c8d9"
down_revision: Union[str, Sequence[str], None] = "db642c952bcf"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "organization_mcp_integration",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("domain", sa.String(length=50), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name="fk_organization_mcp_integration_organization_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "domain",
            name="uq_organization_mcp_integration_organization_id_domain",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("organization_mcp_integration")
