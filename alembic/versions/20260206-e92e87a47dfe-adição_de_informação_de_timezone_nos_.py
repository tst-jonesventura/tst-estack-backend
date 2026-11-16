"""adição de informação de timezone nos campos do tipo datetime

Revision ID: e92e87a47dfe
Revises: 3bc75ef5e1f1
Create Date: 2026-02-06 13:45:34.293362

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import mssql, postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e92e87a47dfe"
down_revision: Union[str, Sequence[str], None] = "3bc75ef5e1f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        dt_type = sa.DateTime(timezone=True)
        existing_type = postgresql.TIMESTAMP()
    elif dialect == "mssql":
        dt_type = mssql.DATETIMEOFFSET()
        existing_type = mssql.DATETIME()
    else:
        dt_type = sa.DateTime(timezone=True)
        existing_type = sa.DateTime()

    op.alter_column(
        "agent",
        "created_at",
        existing_type=existing_type,
        type_=dt_type,
        existing_nullable=False,
    )
    op.alter_column(
        "agent_log",
        "timestamp",
        existing_type=existing_type,
        type_=dt_type,
        existing_nullable=False,
    )
    op.alter_column(
        "agent_run",
        "created_at",
        existing_type=existing_type,
        type_=dt_type,
        existing_nullable=False,
    )
    op.alter_column(
        "document_tech_spec",
        "created_at",
        existing_type=existing_type,
        type_=dt_type,
        existing_nullable=False,
    )
    op.alter_column(
        "document_tech_spec_version",
        "created_at",
        existing_type=existing_type,
        type_=dt_type,
        existing_nullable=True,
        autoincrement=False,
    )
    op.alter_column(
        "document_transcription",
        "created_at",
        existing_type=existing_type,
        type_=dt_type,
        existing_nullable=False,
    )
    op.alter_column(
        "project",
        "created_at",
        existing_type=existing_type,
        type_=dt_type,
        existing_nullable=True,
    )
    op.alter_column(
        "user",
        "created_at",
        existing_type=existing_type,
        type_=dt_type,
        existing_nullable=False,
    )
    op.alter_column(
        "user_access_code",
        "access_code_expiry",
        existing_type=existing_type,
        type_=dt_type,
        existing_nullable=False,
    )
    op.alter_column(
        "user_access_code",
        "created_at",
        existing_type=existing_type,
        type_=dt_type,
        existing_nullable=False,
    )
    op.alter_column(
        "user_project_role",
        "created_at",
        existing_type=existing_type,
        type_=dt_type,
        existing_nullable=False,
    )
    op.alter_column(
        "user_project_role",
        "updated_at",
        existing_type=existing_type,
        type_=dt_type,
        existing_nullable=False,
    )
    if dialect == "mssql":
        op.drop_index("ix_user_story_project_id_timestamp", table_name="user_story")

    op.alter_column(
        "user_story",
        "timestamp",
        existing_type=existing_type,
        type_=dt_type,
        existing_nullable=False,
    )

    if dialect == "mssql":
        op.create_index(
            "ix_user_story_project_id_timestamp",
            "user_story",
            ["project_id", "timestamp"],
            unique=False,
        )
    op.alter_column(
        "user_story_version",
        "timestamp",
        existing_type=existing_type,
        type_=dt_type,
        existing_nullable=True,
        autoincrement=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        dt_type = postgresql.TIMESTAMP()
        existing_type = sa.DateTime(timezone=True)
    elif dialect == "mssql":
        dt_type = mssql.DATETIME()
        existing_type = mssql.DATETIMEOFFSET()
    else:
        dt_type = sa.DateTime()
        existing_type = sa.DateTime()

    op.alter_column(
        "user_story_version",
        "timestamp",
        existing_type=existing_type,
        type_=dt_type,
        existing_nullable=True,
        autoincrement=False,
    )
    if dialect == "mssql":
        op.drop_index("ix_user_story_project_id_timestamp", table_name="user_story")

    op.alter_column(
        "user_story",
        "timestamp",
        existing_type=existing_type,
        type_=dt_type,
        existing_nullable=False,
    )

    if dialect == "mssql":
        op.create_index(
            "ix_user_story_project_id_timestamp",
            "user_story",
            ["project_id", "timestamp"],
            unique=False,
        )
    op.alter_column(
        "user_project_role",
        "updated_at",
        existing_type=existing_type,
        type_=dt_type,
        existing_nullable=False,
    )
    op.alter_column(
        "user_project_role",
        "created_at",
        existing_type=existing_type,
        type_=dt_type,
        existing_nullable=False,
    )
    op.alter_column(
        "user_access_code",
        "created_at",
        existing_type=existing_type,
        type_=dt_type,
        existing_nullable=False,
    )
    op.alter_column(
        "user_access_code",
        "access_code_expiry",
        existing_type=existing_type,
        type_=dt_type,
        existing_nullable=False,
    )
    op.alter_column(
        "user",
        "created_at",
        existing_type=existing_type,
        type_=dt_type,
        existing_nullable=False,
    )
    op.alter_column(
        "project",
        "created_at",
        existing_type=existing_type,
        type_=dt_type,
        existing_nullable=True,
    )
    op.alter_column(
        "document_transcription",
        "created_at",
        existing_type=existing_type,
        type_=dt_type,
        existing_nullable=False,
    )
    op.alter_column(
        "document_tech_spec_version",
        "created_at",
        existing_type=existing_type,
        type_=dt_type,
        existing_nullable=True,
        autoincrement=False,
    )
    op.alter_column(
        "document_tech_spec",
        "created_at",
        existing_type=existing_type,
        type_=dt_type,
        existing_nullable=False,
    )
    op.alter_column(
        "agent_run",
        "created_at",
        existing_type=existing_type,
        type_=dt_type,
        existing_nullable=False,
    )
    op.alter_column(
        "agent_log",
        "timestamp",
        existing_type=existing_type,
        type_=dt_type,
        existing_nullable=False,
    )
    op.alter_column(
        "agent",
        "created_at",
        existing_type=existing_type,
        type_=dt_type,
        existing_nullable=False,
    )
