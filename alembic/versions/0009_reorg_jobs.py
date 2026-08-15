"""Phase 9 — the re-organisation job table and its bulk-undo index.

Purely additive and fully reversible. It runs against a live database holding
~12,500 real decisions for two real accounts, so it creates one new table and
adds one nullable column; it rewrites no existing row and drops nothing.

* ``CREATE TABLE reorg_jobs`` — one row per "re-organise everything" job, with
  its ledger (``total`` / ``done`` / ``skipped``) and its resume ``cursor``.
* ``ALTER TABLE action_logs ADD COLUMN reorg_job_id TEXT NULL`` +
  ``ix_action_logs_reorg_job`` — so **bulk undo of a whole re-organisation is a
  single indexed query**, not a join through ``decisions``. Existing rows get
  NULL, which is exactly right: they belonged to a triage run, not to a job.

The FK on ``action_logs.reorg_job_id`` is deliberately **not** added on SQLite:
``ALTER TABLE ... ADD COLUMN`` cannot attach a constraint there, and a batch
rewrite of a table holding the entire real audit trail is not a risk worth
taking for a referential check the application already enforces. The index — the
part that makes bulk undo fast — is created on every dialect.

Revision ID: 0009_reorg_jobs
Revises: 0008_review_state_integrity
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_reorg_jobs"
down_revision: Union[str, None] = "0008_review_state_integrity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return column in {c["name"] for c in inspector.get_columns(table)}


def _has_table(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def _has_index(table: str, index: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return index in {i["name"] for i in inspector.get_indexes(table)}


def upgrade() -> None:
    # Idempotent by inspection rather than by try/except: a swallowed exception
    # here would hide a genuinely failed DDL against the real database.
    if not _has_table("reorg_jobs"):
        op.create_table(
            "reorg_jobs",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column(
                "user_id",
                sa.Text(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("status", sa.Text(), nullable=False, server_default="running"),
            sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("done", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("skipped", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("cursor", sa.Text(), nullable=True),
            sa.Column("dry_run", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("run_id", sa.Text(), nullable=True),
            sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
            sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
        )
        op.create_index("ix_reorg_jobs_user_id", "reorg_jobs", ["user_id"])
        op.create_index("ix_reorg_jobs_status", "reorg_jobs", ["status"])
        op.create_index(
            "ix_reorg_jobs_user_status", "reorg_jobs", ["user_id", "status"]
        )

    if not _has_column("action_logs", "reorg_job_id"):
        op.add_column("action_logs", sa.Column("reorg_job_id", sa.Text(), nullable=True))

    if not _has_index("action_logs", "ix_action_logs_reorg_job"):
        op.create_index(
            "ix_action_logs_reorg_job", "action_logs", ["reorg_job_id"]
        )


def downgrade() -> None:
    if _has_index("action_logs", "ix_action_logs_reorg_job"):
        op.drop_index("ix_action_logs_reorg_job", table_name="action_logs")
    if _has_column("action_logs", "reorg_job_id"):
        op.drop_column("action_logs", "reorg_job_id")
    if _has_table("reorg_jobs"):
        op.drop_table("reorg_jobs")
