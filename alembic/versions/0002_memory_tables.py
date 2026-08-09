"""memory: vip_entries + priority_profiles

Additive-only. Does not touch 0001's tables. ``corrections`` and
``sender_profiles`` already exist from Phase 1 and are reused as-is by the
memory tool — no ``rules`` row is ever inserted by this slice.

Revision ID: 0002
Revises: 0001
Create Date: 2026-01-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "vip_entries",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "kind", "value", name="uq_vip_entry"),
    )
    op.create_index("ix_vip_entries_user_id", "vip_entries", ["user_id"])

    op.create_table(
        "priority_profiles",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("priority_profiles")
    op.drop_index("ix_vip_entries_user_id", table_name="vip_entries")
    op.drop_table("vip_entries")
