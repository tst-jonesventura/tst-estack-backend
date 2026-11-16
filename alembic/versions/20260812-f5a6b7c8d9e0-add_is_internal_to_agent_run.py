"""marca agent runs internos, que nao aparecem na aba de processamentos

Turnos do tutor criam um AgentRun apenas para registro de uso de ferramentas e
contabilidade de tokens; nao sao processamentos que o time acompanha.

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
Create Date: 2026-08-12 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f5a6b7c8d9e0"
down_revision: Union[str, Sequence[str], None] = "e4f5a6b7c8d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "agent_run",
        sa.Column(
            "is_internal",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("agent_run", "is_internal")
