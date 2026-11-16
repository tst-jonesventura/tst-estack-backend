"""add status column to tutor_message

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-23 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'tutor_message',
        sa.Column(
            'status',
            sa.String(length=20),
            nullable=False,
            server_default='completed',
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('tutor_message', 'status')
