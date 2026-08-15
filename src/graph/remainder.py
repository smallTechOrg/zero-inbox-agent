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
    "no_never_miss_label",
    "below_threshold",
    "unclassified",
)

#: Statuses that mean the thread has actually left the inbox.
_APPLIED_STATUS = "applied"

#: Phase 9. ``held_by_never_miss`` used to be a terminal bucket — the verdict was
#: "leave it in the inbox", so 227 threads sat there permanently. From Phase 9 the
#: verdict is "archive it under the label that names why", so a never-miss row is
#: only still in the inbox for one of two reasons, and they are reported apart
#: because they need different fixes:
#:
#: * ``no_never_miss_label`` — no never-miss category resolved (the user deleted
#:   People, say), so the reframe **deliberately did not archive it**. Nothing
#:   went wrong; the thread is safe, and the fix is to restore the category.
#: * ``held_by_never_miss`` — a label DID resolve and the archive was attempted
#:   and did not land. Something went wrong, and the run says so.
#:
#: The split is derived from ``proposed_action``: the reframe sets ``archive`` when
#: it resolved a label and leaves the row a ``keep`` when it could not. No schema
#: change, and no way for the two to be conflated.
_LEAVING_ACTIONS = ("archive", "digest")


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
        select(
            Decision.autonomy_state,
            Decision.status,
            Decision.proposed_action,
            func.count(Decision.id),
        )
        .where(Decision.run_id == run_id, Decision.user_id == user_id)
        .group_by(Decision.autonomy_state, Decision.status, Decision.proposed_action)
    ).all()

    for autonomy_state, status, proposed_action, count in rows:
        count = int(count or 0)
        if status == _APPLIED_STATUS:
            # Out of the inbox. Counted once, here, whatever its autonomy state.
            applied += count
            continue
        if autonomy_state == AUTO_ACT:
            distance_to_zero += count
            continue
        if (
            autonomy_state == "held_by_never_miss"
            and proposed_action not in _LEAVING_ACTIONS
        ):
            # The reframe found no never-miss label for it and deliberately left
            # it in the inbox. Reported under its own name, never folded into
            # the "we tried and failed" bucket.
            buckets["no_never_miss_label"] += count
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
        # Phase 9, item zero — the honest count of decisions that were APPLIED to
        # the user's mailbox while carrying a `review_state` that never became
        # `reviewed`. Live at the time of writing: 44. Migration `0008` refuses to
        # rewrite them, because rewriting them would make the database assert a
        # review that never happened; so the number is stated instead.
        #
        # It is deliberately NOT a remainder bucket: those threads are out of the
        # inbox, so folding them in would break
        # `inbox_remaining == sum(remainder) + distance_to_zero`. It is user-wide,
        # not run-scoped, because the rows it counts pre-date this run.
        "unreviewed_applied": unreviewed_applied_count(session, user_id=user_id),
        "remainder": {key: buckets[key] for key in REMAINDER_BUCKETS},
        "failures": failures,
    }


def unreviewed_applied_count(session: Session, *, user_id: str) -> int:
    """Decisions already applied to the mailbox that were never actually reviewed.

    The migration-`0008` counterpart, read live: ``status == "applied"`` and
    ``review_state != "reviewed"``. A row that reads ``review_failed`` or is
    still ``provisional`` and yet has been applied is a mutation that slipped
    past a gate which was passing vacuously (Phase 9, item zero). The class is
    closed going forward; the history is reported, not erased.
    """
    from db.models import Decision

    return int(
        session.execute(
            select(func.count(Decision.id)).where(
                Decision.user_id == user_id,
                Decision.status == _APPLIED_STATUS,
                Decision.review_state != "reviewed",
            )
        ).scalar_one()
        or 0
    )
