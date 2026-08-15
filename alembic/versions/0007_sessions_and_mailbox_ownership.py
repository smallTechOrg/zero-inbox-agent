"""Phase 8 — revocable sessions, global mailbox ownership, last_synced_at.

Implements spec/data.md "Phase 8 migration", statement for statement. This runs
against a live database holding ~12,500 real decisions for two real accounts, so
every statement here is either additive or **guarded**:

1. ``CREATE TABLE user_sessions`` + ``ix_user_sessions_user_active``. No backfill —
   existing ``uid``-only cookies stay valid and gain a row on their next request,
   so nobody is signed out by this migration.
2. **Guarded** global unique index on ``channel_accounts (channel, account_email)``.
   Before creating it the migration looks for an address already owned by more
   than one user. If it finds one it **raises, naming the offending addresses**,
   and changes nothing. It must never resolve the conflict by deleting or
   reassigning a row: which human owns a mailbox is not a decision a migration
   gets to make.
3. ``channel_accounts.last_synced_at`` (nullable, no backfill) — a NULL renders as
   "not synced yet", never as a fabricated date.

Fully reversible; there is no one-way data migration in this revision.

Revision ID: 0007_sessions_and_mailbox_ownership
Revises: 0006_autonomy_policy
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_sessions_and_mailbox_ownership"
down_revision: Union[str, None] = "0006_autonomy_policy"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


class DuplicateMailboxOwnership(RuntimeError):
    """The same mailbox is connected to two different Zero Inbox users.

    Raised instead of resolving the conflict. The message names the exact
    addresses so a human can decide which account keeps each one.
    """


DUPLICATE_OWNERS_SQL = sa.text(
    "SELECT channel, account_email, COUNT(DISTINCT user_id) AS owners "
    "FROM channel_accounts GROUP BY channel, account_email "
    "HAVING COUNT(DISTINCT user_id) > 1"
)


def _assert_no_duplicate_ownership(bind) -> None:
    rows = list(bind.execute(DUPLICATE_OWNERS_SQL))
    if not rows:
        return
    offenders = ", ".join(f"{r[0]}:{r[1]} (owned by {r[2]} users)" for r in rows)
    raise DuplicateMailboxOwnership(
        "Cannot create the global mailbox-ownership index: these mailboxes are "
        f"connected to more than one Zero Inbox user — {offenders}. "
        "Decide which account keeps each mailbox and disconnect it from the "
        "other(s), then re-run this migration. This migration will not choose "
        "for you."
    )


def upgrade() -> None:
    # 1. The session table.
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("user_agent_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("ip_hash", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])
    op.create_index(
        "ix_user_sessions_user_active", "user_sessions", ["user_id", "revoked_at"]
    )

    # 2. Guarded ownership constraint — verify the assumption, never resolve it.
    _assert_no_duplicate_ownership(op.get_bind())
    op.create_index(
        "uq_channel_account_global",
        "channel_accounts",
        ["channel", "account_email"],
        unique=True,
    )

    # 3. Last sync time. No backfill.
    op.add_column(
        "channel_accounts",
        sa.Column("last_synced_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("channel_accounts", "last_synced_at")
    op.drop_index("uq_channel_account_global", table_name="channel_accounts")
    op.drop_index("ix_user_sessions_user_active", table_name="user_sessions")
    op.drop_index("ix_user_sessions_user_id", table_name="user_sessions")
    op.drop_table("user_sessions")
