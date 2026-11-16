"""guarda o texto do tutor como NVARCHAR para nao perder emojis e simbolos

As colunas eram VARCHAR com collation Latin1, entao todo caractere fora do code
page (emojis das matrizes de permissao, caracteres de desenho de arvore) era
gravado como '?' ou aproximado por outro caractere.

Revision ID: 7f71b5bba756
Revises: 4eb3156a9a04
Create Date: 2026-09-17 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7f71b5bba756"
down_revision: Union[str, Sequence[str], None] = "4eb3156a9a04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = (
    ("tutor_message", "content", False),
    ("tutor_conversation", "summary", True),
)


def upgrade() -> None:
    """Upgrade schema."""
    for table, column, nullable in COLUMNS:
        op.alter_column(
            table,
            column,
            existing_type=sa.String(),
            type_=sa.Unicode(),
            existing_nullable=nullable,
            nullable=nullable,
        )


def downgrade() -> None:
    """Downgrade schema."""
    for table, column, nullable in COLUMNS:
        op.alter_column(
            table,
            column,
            existing_type=sa.Unicode(),
            type_=sa.String(),
            existing_nullable=nullable,
            nullable=nullable,
        )
