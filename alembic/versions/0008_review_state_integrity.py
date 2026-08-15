"""Phase 9, item zero — a ``reviewed`` row must have actually been reviewed.

Before Phase 9 the never-miss ``second_pass_reviewer`` audited only
``proposed_action = 'archive'``, but ``finalise_review`` upgraded ``review_state``
across the run's **whole** decision set. A ``digest`` decision therefore read
``reviewed`` having never been audited, and the ``NotReviewedError`` gate in
``tools.actions.apply_decision`` passed vacuously — while ``digest`` reaches the
same ``archive_and_label(remove_label_ids=[INBOX])`` mutation as ``archive``.

Measured on the live database: **171 digest decisions, 44 of them already
applied.**

This revision downgrades the historic lie, and only where it is still safe to:

* ``review_state = 'provisional'`` where the row is ``reviewed``, its
  ``proposed_action`` was **outside the audited scope** (i.e. not ``'archive'``,
  which is all the pre-Phase-9 reviewer ever looked at), and it is **not yet
  applied**. Those rows become un-appliable until a real reviewer pass upgrades
  them — ``review_retry`` is the supported route back.
* Rows already ``status = 'applied'`` are **NOT rewritten**. The mutation happened;
  rewriting the row would erase the evidence that it happened unreviewed. Their
  exact count is logged here and surfaced as the ``unreviewed_applied`` ledger
  figure. The honesty rule outranks a tidy migration.

Idempotent and re-runnable: the UPDATE is a no-op on a second pass because no
matching row is left. ``downgrade()`` is deliberately a no-op with an explanation
— see below.

Revision ID: 0008_review_state_integrity
Revises: 0007_sessions_and_mailbox_ownership
"""

from __future__ import annotations

import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_review_state_integrity"
down_revision: Union[str, None] = "0007_sessions_and_mailbox_ownership"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

log = logging.getLogger("alembic.runtime.migration")

#: The only ``proposed_action`` the pre-Phase-9 reviewer ever audited. Everything
#: else that was marked ``reviewed`` was marked so by the run-wide upgrade.
AUDITED_ACTIONS_BEFORE_PHASE_9 = ("archive",)

_COUNT_UNREVIEWED_APPLIED = sa.text(
    """
    SELECT COUNT(*) FROM decisions
     WHERE review_state = 'reviewed'
       AND proposed_action NOT IN ('archive')
       AND status = 'applied'
    """
)

_COUNT_DOWNGRADABLE = sa.text(
    """
    SELECT COUNT(*) FROM decisions
     WHERE review_state = 'reviewed'
       AND proposed_action NOT IN ('archive')
       AND status <> 'applied'
    """
)

_DOWNGRADE_UNAUDITED = sa.text(
    """
    UPDATE decisions
       SET review_state = 'provisional'
     WHERE review_state = 'reviewed'
       AND proposed_action NOT IN ('archive')
       AND status <> 'applied'
    """
)


def _table_exists(bind, name: str) -> bool:
    return name in sa.inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "decisions"):
        # A brand-new database created straight from the models: nothing historic
        # to correct. Still a valid, idempotent no-op.
        log.info("0008_review_state_integrity: no decisions table; nothing to do")
        return

    unreviewed_applied = int(bind.execute(_COUNT_UNREVIEWED_APPLIED).scalar() or 0)
    downgradable = int(bind.execute(_COUNT_DOWNGRADABLE).scalar() or 0)

    result = bind.execute(_DOWNGRADE_UNAUDITED)
    downgraded = getattr(result, "rowcount", downgradable)
    if downgraded is None or downgraded < 0:
        downgraded = downgradable

    # Reported, never silently swallowed: these rows were mutated in Gmail without
    # ever having been reviewed. They are left exactly as they are.
    log.warning(
        "0008_review_state_integrity: downgraded %s unaudited 'reviewed' rows to "
        "'provisional'; left %s already-applied unreviewed rows untouched "
        "(unreviewed_applied=%s)",
        downgraded,
        unreviewed_applied,
        unreviewed_applied,
    )


def downgrade() -> None:
    """Deliberately a no-op, and that is the reversible-safe direction.

    ``upgrade()`` moved rows from a false ``reviewed`` to an honest
    ``provisional``. Reversing it would re-assert that threads nothing ever
    audited had been audited — i.e. it would re-open the safety hole and make
    those rows appliable again. There is also no record of which rows were
    changed to reverse *precisely*, so a blanket re-upgrade would be strictly
    worse than the original defect.

    The schema is unchanged by this revision, so stepping down past it is
    structurally safe; only the (safe-direction) data correction persists.
    """
    log.info(
        "0008_review_state_integrity: downgrade is a no-op — refusing to re-mark "
        "never-audited decisions as 'reviewed'"
    )
