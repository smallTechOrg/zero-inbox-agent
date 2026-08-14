"""decisions.review_state — the never-miss finality gate (Phase 6).

See spec/data.md "Phase 6 migration". Adds ``decisions.review_state`` defaulting to
``provisional``, BACKFILLS every pre-existing row to ``reviewed`` (those rows belong to
runs that already completed their reviewer pass — leaving them provisional would make
historical decisions permanently un-appliable and un-undoable), and indexes
``(run_id, review_state)`` for the resume and apply-eligibility queries.

Revision ID: 0005_decision_review_state
Revises: 0002
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_decision_review_state"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "decisions",
        sa.Column(
            "review_state",
            sa.Text(),
            nullable=False,
            server_default="provisional",
        ),
    )
    # Backfill: everything written before this migration was persisted only at the
    # end of the graph, i.e. after the reviewer pass had already run.
    op.execute(sa.text("UPDATE decisions SET review_state = 'reviewed'"))
    op.create_index(
        "ix_decisions_run_review", "decisions", ["run_id", "review_state"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_decisions_run_review", table_name="decisions")
    op.drop_column("decisions", "review_state")
