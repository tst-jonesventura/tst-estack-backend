"""create tutor conversation tables

Revision ID: a1b2c3d4e5f6
Revises: 5f3e8160519b
Create Date: 2026-07-22 15:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '5f3e8160519b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'tutor_conversation',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('project_id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('summary', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['project_id'], ['project.id'],
            name='fk_tutor_conversation_project_id', ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['user_id'], ['user.id'], name='fk_tutor_conversation_user_id',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'project_id', 'user_id', name='uq_tutor_conversation_project_user',
        ),
    )
    op.create_index(
        'ix_tutor_conversation_project_user',
        'tutor_conversation',
        ['project_id', 'user_id'],
        unique=False,
    )
    op.create_table(
        'tutor_message',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('conversation_id', sa.String(length=36), nullable=False),
        sa.Column('role', sa.String(length=20), nullable=False),
        sa.Column('content', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['conversation_id'], ['tutor_conversation.id'],
            name='fk_tutor_message_conversation_id', ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_tutor_message_conversation_id',
        'tutor_message',
        ['conversation_id'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_tutor_message_conversation_id', table_name='tutor_message')
    op.drop_table('tutor_message')
    op.drop_index(
        'ix_tutor_conversation_project_user', table_name='tutor_conversation'
    )
    op.drop_table('tutor_conversation')
