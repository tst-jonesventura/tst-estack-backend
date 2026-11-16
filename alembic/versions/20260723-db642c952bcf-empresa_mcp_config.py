"""empresa (organization) mcp config: descricao/mcp_provider, tabela de config criptografada do github e associacao obrigatoria de projeto a empresa

Revision ID: db642c952bcf
Revises: c3d4e5f6a7b8
Create Date: 2026-07-23 14:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "db642c952bcf"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1) Company (organization) gains a description and the active MCP provider.
    op.add_column("organization", sa.Column("description", sa.String(), nullable=True))
    op.add_column(
        "organization", sa.Column("mcp_provider", sa.String(length=50), nullable=True)
    )

    # 2) Per-provider (GitHub) encrypted system configuration.
    op.create_table(
        "organization_github_mcp_config",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("github_app_id", sa.String(), nullable=False),
        sa.Column("github_app_private_key_base64", sa.String(), nullable=False),
        sa.Column("mcp_url", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name="fk_organization_github_mcp_config_organization_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id", name="uq_organization_github_mcp_config_organization_id"
        ),
    )

    # 3) Enforce that every project belongs to a company.
    #    Data migration (attaching orphan projects to the default company) is
    #    handled separately by scripts.backfill_project_company, which must run
    #    before this migration when orphan projects still exist.
    with op.batch_alter_table("project") as batch_op:
        batch_op.alter_column(
            "organization_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("project") as batch_op:
        batch_op.alter_column(
            "organization_id",
            existing_type=sa.String(length=36),
            nullable=True,
        )
    op.drop_table("organization_github_mcp_config")
    op.drop_column("organization", "mcp_provider")
    op.drop_column("organization", "description")
