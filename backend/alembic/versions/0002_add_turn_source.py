"""add turns.source

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Every turn recorded before this column existed came from the mic.
    with op.batch_alter_table("turns") as batch:
        batch.add_column(
            sa.Column("source", sa.String(length=16), nullable=False, server_default="voice")
        )


def downgrade() -> None:
    with op.batch_alter_table("turns") as batch:
        batch.drop_column("source")
