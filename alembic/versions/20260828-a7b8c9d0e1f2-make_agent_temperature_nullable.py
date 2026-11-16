"""agent.temperature passa a aceitar NULL

Modelos de reasoning (familia gpt-5) rejeitam o parametro temperature. NULL
significa "nao enviar", e passa a ser o valor do agente tutor.

Revision ID: a7b8c9d0e1f2
Revises: 577557d39b12
Create Date: 2026-08-28 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, Sequence[str], None] = "577557d39b12"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("agent") as batch_op:
        batch_op.alter_column(
            "temperature", existing_type=sa.Float(), nullable=True
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("UPDATE agent SET temperature = 0.3 WHERE temperature IS NULL")
    with op.batch_alter_table("agent") as batch_op:
        batch_op.alter_column(
            "temperature", existing_type=sa.Float(), nullable=False
        )
