"""create transaction_id_seq for sqlalchemy_continuum

Revision ID: b3e1f5a7c2d4
Revises: a2f4c6e8d1b3
Create Date: 2026-11-09 16:30:00.000000

"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b3e1f5a7c2d4"
down_revision: str | Sequence[str] | None = "a2f4c6e8d1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create transaction_id_seq sequence if not exists."""
    bind = op.get_bind()
    dialect_name = bind.dialect.name if bind else ""

    if dialect_name == "mssql":
        op.execute(
            """
            IF NOT EXISTS (SELECT * FROM sys.sequences WHERE name = 'transaction_id_seq')
            BEGIN
                CREATE SEQUENCE transaction_id_seq START WITH 1 INCREMENT BY 1;
            END
            """
        )
    elif dialect_name == "postgresql":
        op.execute("CREATE SEQUENCE IF NOT EXISTS transaction_id_seq START WITH 1 INCREMENT BY 1;")
    else:
        try:
            op.execute("CREATE SEQUENCE transaction_id_seq START WITH 1 INCREMENT BY 1;")
        except Exception:
            pass


def downgrade() -> None:
    """Drop transaction_id_seq sequence."""
    bind = op.get_bind()
    dialect_name = bind.dialect.name if bind else ""

    if dialect_name == "mssql":
        op.execute(
            """
            IF EXISTS (SELECT * FROM sys.sequences WHERE name = 'transaction_id_seq')
            BEGIN
                DROP SEQUENCE transaction_id_seq;
            END
            """
        )
    elif dialect_name == "postgresql":
        op.execute("DROP SEQUENCE IF EXISTS transaction_id_seq;")
    else:
        try:
            op.execute("DROP SEQUENCE transaction_id_seq;")
        except Exception:
            pass
