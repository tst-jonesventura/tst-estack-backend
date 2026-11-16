"""marca quantas mensagens ja entraram no resumo da conversa do tutor

Permite dobrar apenas as mensagens recem-evictadas no resumo existente, em vez de
re-resumir todo o historico a cada turno.

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-08-31 11:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9d0e1f2a3b4"
down_revision: Union[str, Sequence[str], None] = "b8c9d0e1f2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "tutor_conversation",
        sa.Column(
            "summarized_message_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("tutor_conversation", "summarized_message_count")
