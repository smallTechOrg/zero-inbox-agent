"""The remainder ledger — the single honest answer to "how far from zero am I, and why?".

One pure read function. It is queried **live** from ``decisions`` on every call and
never reads a cached count, because a cached count is exactly how run ``fbeed060``
reported itself a clean success while 615 archives sat unapplied.

Spec: spec/capabilities/drive-to-inbox-zero.md Rule E1, spec/api.md § Phase 7.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

_log = logging.getLogger("zero_inbox.remainder")

#: ``autonomy_state`` of a decision the agent decided it may act on by itself.
AUTO_ACT = "auto_act"

#: The buckets that make up the inbox after a run, in the order the UI renders them.
#: ``unclassified`` is NOT a healthy bucket: it counts rows whose ``autonomy_state``
#: is NULL (every decision written before Phase 7 — 11,449 of them in production).
#: They are always reported under their own name and never folded into a healthy
#: bucket, because fabricating history is worse than admitting we do not know.
REMAINDER_BUCKETS = (
    "needs_your_call",
    "category_keep",
    "held_by_never_miss",
    "below_threshold",
    "unclassified",
)

#: Statuses that mean the thread has actually left the inbox.
_APPLIED_STATUS = "applied"


def remainder_ledger(session: Session, *, run_id: str, user_id: str) -> dict[str, Any]:
    """Return the live remainder ledger for one run, scoped to ``user_id``.

    Invariant, asserted by ``tests/unit/graph/test_remainder.py``::

        inbox_remaining == sum(remainder.values()) + distance_to_zero

    ``distance_to_zero`` is the count of decisions the agent itself decided should
    leave the inbox and that are still in it — 0 on a healthy run, and 615 on the
    run that motivated this phase.
    """
    from db.models import Decision, TriageRun

    applied = 0
    distance_to_zero = 0
    buckets: dict[str, int] = dict.fromkeys(REMAINDER_BUCKETS, 0)

    # One grouped SELECT. ORM column expressions only — no raw SQL string, so the
    # NULL grouping behaves identically on SQLite and PostgreSQL.
    rows = session.execute(
        select(Decision.autonomy_state, Decision.status, func.count(Decision.id))
        .where(Decision.run_id == run_id, Decision.user_id == user_id)
        .group_by(Decision.autonomy_state, Decision.status)
    ).all()

    for autonomy_state, status, count in rows:
        count = int(count or 0)
        if status == _APPLIED_STATUS:
            # Out of the inbox. Counted once, here, whatever its autonomy state.
            applied += count
            continue
        if autonomy_state == AUTO_ACT:
            distance_to_zero += count
            continue
        bucket = autonomy_state if autonomy_state in buckets else None
        if bucket is None:
            if autonomy_state:
                # An autonomy_state we do not know about must still be reported,
                # never dropped — dropping it would silently break the invariant.
                _log.warning(
                    "remainder.unknown_autonomy_state run_id=%s state=%r count=%d",
                    run_id,
                    autonomy_state,
                    count,
                )
            bucket = "unclassified"
        buckets[bucket] += count

    run = session.get(TriageRun, run_id)
    if run is not None and run.user_id != user_id:
        run = None  # another user's run is not readable here, and never leaks
    apply_ledger = ((run.counts or {}).get("apply") if run is not None else None) or {}

    apply_failed_reason = apply_ledger.get("apply_failed_reason") or None
    failures = [
        {"decision_id": str(f.get("decision_id") or ""), "error": str(f.get("error") or "")}
        for f in (apply_ledger.get("failures") or [])
    ]
    dry_run = bool(apply_ledger.get("dry_run", bool(run.dry_run) if run is not None else False))

    inbox_remaining = sum(buckets.values()) + distance_to_zero

    return {
        "run_id": str(run_id),
        "inbox_remaining": inbox_remaining,
        "distance_to_zero": distance_to_zero,
        "applied": applied,
        # spec/api.md: false when the apply pass recorded a reason OR the inbox did
        # not actually move. A run that archived nothing can never render as clean.
        "apply_ok": apply_failed_reason is None and distance_to_zero == 0,
        "apply_failed_reason": apply_failed_reason,
        "dry_run": dry_run,
        "remainder": {key: buckets[key] for key in REMAINDER_BUCKETS},
        "failures": failures,
    }
