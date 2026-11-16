"""liga tutor_message ao agent_run do turno

Permite medir vida do turno pela atividade real (AgentLog do run) em vez de
tempo decorrido desde a criacao do placeholder.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-08-31 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8c9d0e1f2a3"
down_revision: Union[str, Sequence[str], None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "tutor_message",
        sa.Column("agent_run_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_tutor_message_agent_run_id",
        "tutor_message",
        "agent_run",
        ["agent_run_id"],
        ["id"],
    )
    op.create_index(
        "ix_tutor_message_conversation_status",
        "tutor_message",
        ["conversation_id", "status"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_tutor_message_conversation_status", table_name="tutor_message")
    op.drop_constraint(
        "fk_tutor_message_agent_run_id", "tutor_message", type_="foreignkey"
    )
    op.drop_column("tutor_message", "agent_run_id")
