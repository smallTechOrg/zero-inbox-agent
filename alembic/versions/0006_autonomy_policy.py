"""Phase 7 autonomy policy — make ``auto_act_threshold`` real, and record why.

Implements spec/data.md "Phase 7 migration", statement for statement. **This
touches real user rows** (``zero_inbox.db`` holds 11,449 real decisions for two
real accounts), so nothing here is improvised:

1. ``categories.auto_act_threshold`` (nullable) — NULL inherits the global bar.
2. Seed ``outreach`` / ``receipts`` at 0.85 for users who already exist (new users
   get the same values from ``db.seed.SEEDED_AUTO_ACT_THRESHOLDS``). Both bars are
   live, because both categories are ``archive`` as of this phase.
2b. Flip ``receipts.default_action`` from ``keep`` to ``archive``, guarded on the
   current value being the untouched seeded ``keep`` so a user's own choice is
   never overwritten. Receipts are archival records, not work; keeping them put a
   ~715-thread floor under the inbox. A time-sensitive receipt is still held by
   the never-miss layer.
3. ``decisions.autonomy_state`` (nullable). **No backfill** — a pre-Phase-7
   decision was made under a policy that did not exist, and inventing a state for
   it would fabricate history. Every such row is reported by the remainder ledger
   under the explicit ``unclassified`` bucket.
4. Index ``(run_id, autonomy_state)`` — the remainder ledger and the
   ``distance_to_zero`` query both use it.
5. ``UPDATE user_settings SET auto_act_threshold = 0.80 WHERE auto_act_threshold > 0.90``
   — the load-bearing statement. A value above 0.90 was never read by any decision
   or apply code path, so it never expressed a real user preference: it was the
   unused shipped default (0.95), and it sits above the model's entire measured
   output range (~0.94 ceiling). Resetting it cannot regress behaviour because no
   behaviour was ever derived from it, and leaving it would mean this fix silently
   does nothing for the exact account that reported the problem. Values at or
   below 0.90 are left **exactly** as they are — the second real account holds
   0.75, a deliberate "act on everything above the floor" setting, and it survives
   untouched.
6. The column default drops from 0.95 to 0.80.

Revision ID: 0006_autonomy_policy
Revises: 0005_decision_review_state
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_autonomy_policy"
down_revision: Union[str, None] = "0005_decision_review_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Per-category autonomy bar. NULL means "inherit the global".
    op.add_column(
        "categories", sa.Column("auto_act_threshold", sa.Float(), nullable=True)
    )

    # 2. Seed the per-category overrides for users that already exist.
    op.execute(
        sa.text(
            "UPDATE categories SET auto_act_threshold = 0.85 "
            "WHERE key IN ('outreach', 'receipts')"
        )
    )

    # 2b. Flip Receipts to `archive` for users that already exist. The
    #     `AND default_action = 'keep'` guard is load-bearing: it migrates only
    #     the untouched seeded value, so a user who deliberately set Receipts to
    #     `digest` (or back to `keep` after this ships) is never overwritten.
    op.execute(
        sa.text(
            "UPDATE categories SET default_action = 'archive' "
            "WHERE key = 'receipts' AND default_action = 'keep'"
        )
    )

    # 3. The autonomy lifecycle column. Deliberately NOT backfilled.
    op.add_column("decisions", sa.Column("autonomy_state", sa.Text(), nullable=True))

    # 4. The remainder-ledger / distance-to-zero index.
    op.create_index(
        "ix_decisions_run_autonomy", "decisions", ["run_id", "autonomy_state"], unique=False
    )

    # 5. Migrate the persisted global threshold. One-way, and justified above.
    op.execute(
        sa.text(
            "UPDATE user_settings SET auto_act_threshold = 0.80 "
            "WHERE auto_act_threshold > 0.90"
        )
    )

    # 6. The column default follows the ORM default (0.95 -> 0.80).
    with op.batch_alter_table("user_settings") as batch:
        batch.alter_column(
            "auto_act_threshold",
            existing_type=sa.Float(),
            existing_nullable=False,
            server_default=sa.text("0.8"),
        )


def downgrade() -> None:
    # NOTE: step 5 of the upgrade is a deliberate ONE-WAY data migration. The
    # pre-upgrade per-row ``user_settings.auto_act_threshold`` values are not
    # restored, because they are not recoverable — guessing "it was probably
    # 0.95" would invent a preference the same way the upgrade refuses to invent
    # an ``autonomy_state``. Only the column DEFAULT is restored.
    with op.batch_alter_table("user_settings") as batch:
        batch.alter_column(
            "auto_act_threshold",
            existing_type=sa.Float(),
            existing_nullable=False,
            server_default=sa.text("0.95"),
        )
    # Step 2b IS reversible and is reversed: Receipts goes back to `keep`.
    op.execute(
        sa.text(
            "UPDATE categories SET default_action = 'keep' "
            "WHERE key = 'receipts' AND default_action = 'archive'"
        )
    )
    op.drop_index("ix_decisions_run_autonomy", table_name="decisions")
    op.drop_column("decisions", "autonomy_state")
    op.drop_column("categories", "auto_act_threshold")
