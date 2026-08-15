"""Review recovery — re-enter the never-miss gate for one completed run.

See spec/capabilities/review-recovery.md and spec/ui.md screen 24.

The reviewer is allowed to fail: a provider outage did exactly that on a real
2,122-thread run. When it does, those decisions stay ``provisional`` /
``review_failed`` and ``tools.actions.apply_decision`` refuses them forever —
correctly. ``POST /api/runs/{id}/apply`` therefore cannot clear the state, and
until now the only recovery was throwing the whole run away and re-classifying
the entire mailbox.

This module is the missing **re-entry point into the existing gate**, never a
parallel path around it:

* it calls the SAME reviewer node (``second_pass_reviewer``) and the SAME
  never-miss floor/reply-history/VIP chain (``apply_never_miss_floor``) as a
  normal run, in the same fixed order;
* it then calls the SAME apply pass (``graph.nodes.apply_run_decisions``), which
  passes ``force=False`` and re-checks ``review_state`` inside
  ``apply_decision`` — a thread reaches the Gmail mutator only after a real
  reviewer pass;
* it **never** writes ``review_state = "reviewed"``. The only write this module
  makes to that column is the *downgrade* ``review_failed -> provisional``
  (:func:`_reenter_review_gate`) that puts the rows back at the gate's entrance.
  Both values are refused by ``apply_decision``, so this write can never unblock
  a mutation; only the reviewer's own persistence writer (``finalise_review``,
  invoked from inside the never-miss chain) can ever produce ``reviewed``.
* it re-classifies nothing. No tier-1..4 node runs, so a retry over 120 threads
  costs one reviewer pass, not a second triage.
"""

from __future__ import annotations

from typing import Any

from observability.events import get_logger

log = get_logger("review_retry")

#: The two review states that mean "never got past the reviewer". Both are
#: refused by ``apply_decision``; neither can ever reach a Gmail mutation.
PENDING_REVIEW_STATES = ("provisional", "review_failed")

#: Returned unchanged when there is nothing to retry — a no-op is a success,
#: not an error (the capability spec's idempotence rule).
ZERO_COUNTS: dict[str, int] = {"retried": 0, "reviewed": 0, "still_failed": 0, "applied": 0}


def _decision_state(row: Any) -> dict:
    """One persisted decision in the shape the graph nodes expect.

    Read while the row is still attached to its session — the caller detaches
    from the DB before the LLM pass, which can take minutes.
    """
    return {
        "item_id": row.item_id,
        "category_id": row.category_id or "",
        "proposed_action": row.proposed_action,
        "confidence": float(row.confidence or 0.0),
        "reasoning": row.reasoning or "",
        "decided_by": row.decided_by or "llm",
        # Carried through verbatim: ``finalise_review`` writes ``status`` back,
        # so dropping it here would silently resurrect a ``needs_your_call`` row.
        "status": row.status or "proposed",
        "time_sensitive": bool(getattr(row, "time_sensitive", False)),
    }


def _item_state(row: Any) -> dict:
    """One persisted item — headers, subject and the redacted snippet only.

    No message body exists in the schema and none is added here: the reviewer
    sees exactly what it saw on the original pass.
    """
    return {
        "id": row.id,
        "external_thread_id": row.external_thread_id,
        "subject": row.subject or "",
        "from_name": row.from_name or "",
        "from_email": row.from_email or "",
        "from_domain": row.from_domain or "",
        "snippet_redacted": row.snippet_redacted or "",
        "internal_date": row.internal_date.isoformat() if row.internal_date else None,
        "is_unread": bool(row.is_unread),
        "list_id": row.list_id,
    }


def _load_pending(session, *, run_id: str, user_id: str) -> tuple[list, list]:
    """The run's non-``reviewed`` decisions and their items, user-scoped."""
    from sqlalchemy import select

    from db.models import Decision, Item

    decisions = list(
        session.execute(
            select(Decision).where(
                Decision.run_id == run_id,
                Decision.user_id == user_id,
                Decision.review_state.in_(PENDING_REVIEW_STATES),
            )
        ).scalars()
    )
    if not decisions:
        return [], []
    items = list(
        session.execute(
            select(Item).where(
                Item.user_id == user_id,
                Item.id.in_(sorted({d.item_id for d in decisions})),
            )
        ).scalars()
    )
    return decisions, items


def _reenter_review_gate(session, *, run_id: str, user_id: str, item_ids: list[str]) -> int:
    """Put ``review_failed`` rows back to ``provisional`` — the gate's entrance.

    This is the ONLY ``review_state`` write in this module, and it is a
    downgrade in the safe direction. ``finalise_review`` (the reviewer's own
    writer) upgrades ``provisional -> reviewed`` and never
    ``review_failed -> reviewed``, so without this step a row that failed once
    could never recover no matter how many times the reviewer passed it.

    The ``WHERE`` clause makes the unsafe write unrepresentable: it can only
    match rows of THIS run and user that are currently ``review_failed``, and
    the only value it can write is ``provisional`` — a state
    ``apply_decision`` refuses exactly as hard as ``review_failed``.
    """
    if not item_ids:
        return 0
    from sqlalchemy import update

    from db.models import Decision

    changed = session.execute(
        update(Decision)
        .where(
            Decision.run_id == run_id,
            Decision.user_id == user_id,
            Decision.review_state == "review_failed",
            Decision.item_id.in_(sorted(set(item_ids))),
        )
        .values(review_state="provisional")
    ).rowcount
    session.commit()
    return int(changed or 0)


def _count_states(session, *, run_id: str, user_id: str, item_ids: list[str]) -> dict[str, int]:
    from sqlalchemy import func, select

    from db.models import Decision

    if not item_ids:
        return {}
    rows = session.execute(
        select(Decision.review_state, func.count(Decision.id))
        .where(
            Decision.run_id == run_id,
            Decision.user_id == user_id,
            Decision.item_id.in_(sorted(set(item_ids))),
        )
        .group_by(Decision.review_state)
    ).all()
    return {str(state): int(count or 0) for state, count in rows}


def retry_review(*, run_id: str, user_id: str) -> dict:
    """Re-review this run's non-``reviewed`` decisions, then apply what passes.

    Returns ``{"retried", "reviewed", "still_failed", "applied"}``. Never raises
    into its caller: the API route runs it as a background task, and a failure
    must leave the rows blocked and the amber bar up, never take the server down.
    """
    from db.models import TriageRun
    from db.session import create_db_session

    counts = dict(ZERO_COUNTS)
    try:
        with create_db_session() as session:
            run = session.get(TriageRun, run_id)
            if run is None or run.user_id != user_id:
                # Another user's run is not visible here either — a run id is
                # not a capability, on the route or in the worker.
                return dict(ZERO_COUNTS)
            channel_account_id = run.channel_account_id
            dry_run = bool(run.dry_run)

            rows, item_rows = _load_pending(session, run_id=run_id, user_id=user_id)
            if not rows:
                return dict(ZERO_COUNTS)

            item_ids = [r.item_id for r in rows]
            decisions_before = {r.item_id: r.proposed_action for r in rows}
            pending = [_decision_state(r) for r in rows]
            items = [_item_state(i) for i in item_rows]

        state: dict = {
            "run_id": run_id,
            "user_id": user_id,
            "channel_account_id": channel_account_id or "",
            "dry_run": dry_run,
            "items": items,
        }

        from graph.nodes import load_context

        context = load_context(state)
        context.pop("error", None)
        state.update(context)

        category_key_by_id = {
            str(c.get("id")): str(c.get("key") or "")
            for c in (state.get("categories") or [])
            if c.get("id")
        }
        state["decisions"] = [
            {
                **{k: v for k, v in d.items() if k != "category_id"},
                "category": category_key_by_id.get(d["category_id"], ""),
            }
            for d in pending
        ]

        log.info(
            "triage.retry_review_started",
            run_id=run_id,
            user_id=user_id,
            retried=len(pending),
            dry_run=dry_run,
        )

        with create_db_session() as session:
            _reenter_review_gate(
                session, run_id=run_id, user_id=user_id, item_ids=item_ids
            )

        # The real gate, in the real order: reviewer -> floor -> reply history ->
        # VIP. ``apply_never_miss_floor`` is what persists the reviewer's verdict
        # (via ``finalise_review``); nothing here writes ``reviewed`` itself.
        from graph.nodes_review import apply_never_miss_floor, second_pass_reviewer

        reviewed_out = second_pass_reviewer(state)
        state["decisions"] = reviewed_out.get("decisions") or state["decisions"]
        state["review_failed_item_ids"] = list(
            reviewed_out.get("review_failed_item_ids") or []
        )
        floor_out = apply_never_miss_floor(state)
        state["decisions"] = floor_out.get("decisions") or state["decisions"]

        with create_db_session() as session:
            by_state = _count_states(
                session, run_id=run_id, user_id=user_id, item_ids=item_ids
            )
        counts["retried"] = len(pending)
        counts["reviewed"] = by_state.get("reviewed", 0)
        # Anything not upgraded is still blocked, whatever its exact state:
        # ``provisional`` (the reviewer never got to it) counts as still failed
        # for the user, because the outcome is identical — it stays in the inbox.
        counts["still_failed"] = sum(
            n for s, n in by_state.items() if s != "reviewed"
        )

        # The ordinary apply pass. Same function the graph's ``finalize`` calls:
        # ``force=False``, ``review_state`` untouched, dry-run absolute.
        from graph.nodes import apply_run_decisions

        ledger = apply_run_decisions(
            run_id=run_id,
            user_id=user_id,
            channel_account_id=channel_account_id or "",
            dry_run=dry_run,
        )
        counts["applied"] = int((ledger or {}).get("applied") or 0)

        flipped_to_keep = sum(
            1
            for d in state["decisions"]
            if decisions_before.get(d["item_id"]) == "archive"
            and d.get("proposed_action") != "archive"
        )
        log.info(
            "triage.retry_review_finished",
            run_id=run_id,
            user_id=user_id,
            retried=counts["retried"],
            reviewed=counts["reviewed"],
            still_failed=counts["still_failed"],
            applied=counts["applied"],
            flipped_to_keep=flipped_to_keep,
            dry_run=dry_run,
        )
        return counts
    except Exception as exc:  # never raises into the background task runner
        log.warning(
            "triage.retry_review_failed",
            run_id=run_id,
            user_id=user_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        return counts
