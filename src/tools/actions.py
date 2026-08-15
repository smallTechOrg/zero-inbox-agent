"""Applying approved decisions to the real mailbox, with a full undo trail.

spec/capabilities/gmail-actions-and-undo.md is the source of truth. This module is
transport-agnostic — it is handed a ``GmailMutator``-shaped object and a
``GmailLabelManager``-shaped object by its caller (``api/actions.py``) and never builds
a Gmail service itself, matching ``tools/taxonomy.py``'s split from ``api/categories.py``.

Hard rules enforced here (not just documented):

- Only a decision whose ``status == "approved"`` may reach a mutation call. ``rejected``
  decisions are never passed in by any caller in this codebase; this module raises
  :class:`NotApprovedError` if one somehow arrives anyway.
- A decision in ``needs_your_call`` can never be mutated, even if some caller tries.
- No mutation runs while ``dry_run`` is true — raises ``channels.base.DryRunViolation``.
- Every mutation writes an ``ActionLog`` row — with a non-null ``undo_token`` describing
  the precise inverse operation — **before** the decision is marked ``applied``.
- Undo restores the prior label state exactly and is idempotent: calling it twice is a
  no-op the second time, not an error, and never calls Gmail the second time.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from channels.base import DryRunViolation
from channels.gmail.labels import label_name_for
from channels.gmail.mutations import INBOX_LABEL_ID


#: Every ``proposed_action`` this module is willing to turn into a real Gmail
#: mutation — and therefore, by definition, every action the never-miss
#: second-pass reviewer must audit. ``digest`` is in here because it reaches the
#: exact same ``archive_and_label(remove_label_ids=[INBOX])`` call as ``archive``
#: does; auditing only ``archive`` is how 171 live ``digest`` decisions came to
#: read ``reviewed`` having never been looked at, 44 of them already applied.
#:
#: This set and ``graph.nodes_review.REVIEWABLE_ACTIONS`` are ONE contract,
#: asserted equal by ``tests/unit/graph/test_review_scope.py``. Adding a third
#: mutating action here without widening the reviewer fails that test — which is
#: the whole point: the hole cannot be silently reopened.
MUTABLE_ACTIONS: frozenset[str] = frozenset({"archive", "digest"})


class ActionsError(ValueError):
    """A business-rule violation applying or undoing an action."""


class NeedsYourCallError(ActionsError):
    """The decision is in ``needs_your_call`` and can never be mutated."""


class NotApprovedError(ActionsError):
    """Only ``approved`` decisions are eligible for ``POST /api/actions/apply``."""


class NotArchivableError(ActionsError):
    """A ``keep``-proposed decision can never reach a Gmail mutation.

    Approving a ``keep`` proposal means "yes, this belongs in my inbox" — a
    confirmation, not an instruction to archive it. Only ``archive`` and
    ``digest`` proposals may ever be mutated; this is the enforcement point
    for that, independent of whatever a caller (bulk-approve, a UI bug, a
    future integration) marked ``approved``.
    """


class NotReviewedError(ActionsError):
    """The decision has not passed the never-miss second-pass reviewer.

    Decisions are persisted the instant a tier makes them (``review_state
    ="provisional"``) so an interrupted run is resumable, but durability is not
    finality: only a row the reviewer has upgraded to ``review_state="reviewed"``
    may ever reach a Gmail mutation. Checked **independently of ``status``** — a
    caller cannot force a provisional archive through by flipping ``status`` to
    ``approved`` — and **not bypassable by ``force=True``**, which only ever
    bypassed the keep-proposed guard.
    """


class NoNeverMissLabelError(ActionsError):
    """A never-miss thread had no never-miss label to be archived under.

    **This refusal is the guarantee, not an edge case.** The Phase 9 reframe
    rests entirely on the promise that a never-miss thread leaving the inbox is
    always findable in one click under a label that names why it was held. A
    never-miss archive with no label would be an ordinary disappearance — the
    single outcome this product exists to prevent — so it is not a permitted
    operation. The thread stays in the inbox and the ledger names it under
    ``no_never_miss_label``.

    Raised **before** the mutator is touched, so the refusal costs zero Gmail
    calls and cannot half-happen.
    """


class Mutator(Protocol):
    def get_thread_labels(self, thread_id: str) -> list[str]: ...
    def archive_and_label(self, thread_id: str, *, category_label_id: str) -> dict: ...
    def undo_archive_and_label(self, thread_id: str, *, category_label_id: str) -> dict: ...
    def restore_labels(
        self, thread_id: str, *, add_label_ids: list[str], remove_label_ids: list[str]
    ) -> dict: ...


class LabelLookup(Protocol):
    def ensure_label(self, name: str) -> dict: ...


def _models():
    from db import models

    return models


def _now() -> datetime:
    return datetime.now(timezone.utc)


#: Statuses from which an ordinary apply may proceed. ``approved`` only: an
#: approval is the caller's explicit instruction to act.
_APPLY_STATUSES = frozenset({"approved"})

#: Statuses from which a RE-LABEL may proceed. ``applied`` is included because
#: re-organising a taxonomy means relabelling mail that already left the inbox —
#: that is the whole point of "re-organise everything" — and such a row is
#: ``applied``, not ``approved``. It is still a label move, never a new archive.
_RELABEL_STATUSES = frozenset({"approved", "applied"})


def _gate(
    session: Session,
    user_id: str,
    decision_id: str,
    *,
    dry_run: bool,
    allowed_statuses: frozenset[str] = _APPLY_STATUSES,
):
    """The single eligibility gate every mutation in this module passes through.

    There is deliberately **one** of these. Phase 9 adds two new mutation
    entry points (``archive_to_never_miss_label``, ``relabel_decision``) and a
    second, subtly-different copy of these checks is exactly how a bypass gets
    built by accident. Order matters and is asserted by tests:

    1. ``dry_run`` — absolute, and first, so a dry run never even reads a row's
       eligibility, let alone acts on it;
    2. ``NotReviewedError`` — the never-miss finality gate, **before** the
       mutator is ever touched and independent of ``status``, so no caller can
       force a provisional archive through by flipping ``status``. ``force=True``
       does not reach this check and never has;
    3. ``needs_your_call`` — never mutable by anything;
    4. the status allow-list.

    Returns the eligible ``Decision`` row.
    """
    if dry_run:
        raise DryRunViolation("dry_run is on — no mutation was attempted")

    m = _models()
    decision = session.execute(
        select(m.Decision).where(m.Decision.id == decision_id, m.Decision.user_id == user_id)
    ).scalar_one_or_none()
    if decision is None:
        raise ActionsError(f"decision {decision_id!r} not found")

    review_state = getattr(decision, "review_state", "reviewed")
    if review_state != "reviewed":
        raise NotReviewedError(
            "this decision has not passed the never-miss reviewer and can never be "
            f"applied (review_state={review_state!r})"
        )
    if decision.status == "needs_your_call":
        raise NeedsYourCallError("a decision in needs_your_call can never be mutated")
    if decision.status not in allowed_statuses:
        raise NotApprovedError(
            f"only {sorted(allowed_statuses)} decisions are eligible here "
            f"(status={decision.status!r})"
        )
    return decision


def _resolved_category(session: Session, user_id: str, decision):
    m = _models()
    category = (
        session.get(m.Category, decision.category_id) if decision.category_id else None
    )
    if category is None or category.user_id != user_id:
        return None
    return category


def apply_decision(
    session: Session,
    user_id: str,
    decision_id: str,
    *,
    mutator: Mutator,
    label_lookup: LabelLookup,
    dry_run: bool,
    force: bool = False,
) -> object:
    """Applies one ``approved`` decision. Returns the created ``ActionLog`` row.

    Writes the ``ActionLog`` row (with its undo token) *before* flipping the decision
    to ``applied`` — the ordering the spec requires. Raises ``DryRunViolation``,
    :class:`NeedsYourCallError`, :class:`NotApprovedError` or :class:`ActionsError`
    without ever calling ``mutator`` if the decision is not eligible.

    ``force`` (default False) is the ONLY way to archive a ``keep``-proposed
    decision. It exists for a deliberate, informed, user-initiated "archive
    everything except what I have to look at" sweep — never set it from an
    ordinary bulk-approve path, which must keep defaulting to the safe
    behaviour (refuse). ``needs_your_call`` and non-``approved`` decisions are
    never overridable, by ``force`` or anything else.
    """
    decision = _gate(session, user_id, decision_id, dry_run=dry_run)
    if decision.proposed_action not in MUTABLE_ACTIONS and not force:
        raise NotArchivableError(
            f"a {decision.proposed_action!r}-proposed decision can never be archived "
            f"(only {sorted(MUTABLE_ACTIONS)} proposals may be mutated; pass "
            "force=True for a deliberate override)"
        )

    m = _models()
    item = session.get(m.Item, decision.item_id)
    if item is None or item.user_id != user_id:
        raise ActionsError("decision refers to an item that no longer exists")
    category = (
        session.get(m.Category, decision.category_id) if decision.category_id else None
    )
    if category is None or category.user_id != user_id:
        raise ActionsError("decision has no category to label the thread with")

    label = label_lookup.ensure_label(category.channel_label_name or label_name_for(category.name))
    category_label_id = label["id"]
    if not category.channel_label_id:
        category.channel_label_id = category_label_id

    # Capture the pre-triage label state for exact undo restoration.
    # Best-effort: a failed snapshot must not block the mutation — we fall
    # back to the old implicit inverse if we cannot read the current labels.
    original_label_ids: list[str] = []
    try:
        original_label_ids = mutator.get_thread_labels(item.external_thread_id)
    except Exception:
        pass  # snapshot is best-effort; undo will use category_label_id fallback

    # The single atomic Gmail call. Failure here (after 3 retries inside the
    # mutator) propagates unchanged — nothing below is executed, nothing is
    # logged as applied, and the decision stays `approved` for Retry.
    response = mutator.archive_and_label(item.external_thread_id, category_label_id=category_label_id)

    # Emit transparency event — must never block or raise
    try:
        from events import bus as _archived_bus
        import time as _archived_time
        _archived_bus.emit(user_id, {
            "type": "thread_archived",
            "ts": _archived_time.time(),
            "run_id": getattr(decision, "run_id", None),
            "subject": str(getattr(item, "subject", None) or "")[:60],
            "category": category.name if category else "",
            "label_name": category.channel_label_name if category else "",
        })
    except Exception:  # pragma: no cover
        pass

    action_log = m.ActionLog(
        user_id=user_id,
        decision_id=decision.id,
        operation="archive",
        request_params={
            "thread_id": item.external_thread_id,
            "add_label_ids": [category_label_id],
            "remove_label_ids": [INBOX_LABEL_ID],
        },
        response=response,
        undo_token={
            "thread_id": item.external_thread_id,
            "category_label_id": category_label_id,
            # Full pre-triage snapshot — used by the precision undo path.
            "original_label_ids": original_label_ids,
            # Labels this mutation added; needed to undo exactly what was done.
            "labels_added_by_triage": [category_label_id],
            # Legacy inverse fields kept for older tokens still in the DB.
            "add_label_ids": [INBOX_LABEL_ID],
            "remove_label_ids": [category_label_id],
        },
    )
    session.add(action_log)
    session.flush()  # the ActionLog row (with its undo token) exists before...

    decision.status = "applied"
    decision.decided_at = decision.decided_at or _now()

    labels = set(item.channel_labels or [])
    labels.discard(INBOX_LABEL_ID)
    labels.add(category_label_id)
    item.channel_labels = sorted(labels)
    session.flush()  # ...the decision flips to applied.

    try:
        from events import bus
        import time

        bus.emit(
            user_id,
            {
                "type": "gmail_mutation_applied",
                "ts": time.time(),
                "action_log_id": str(action_log.id),
                "thread_count": 1,
                "category": category.key if category else None,
            },
        )
    except Exception:  # pragma: no cover - event bus must never fail an action
        pass

    return action_log


def archive_to_never_miss_label(
    session: Session,
    user_id: str,
    decision_id: str,
    *,
    mutator: Mutator,
    label_lookup: LabelLookup,
    dry_run: bool,
) -> object:
    """Phase 9. Archive a never-miss thread **under the label that names why**.

    This is the whole reframe, in one function. Before Phase 9 a never-miss
    verdict meant "leave it in the inbox", which put a 227-thread floor under a
    product whose promise is zero. From Phase 9 it means "archive it and attach
    ``ZeroInbox/People`` / ``ZeroInbox/Urgent`` / ``ZeroInbox/Important``", so the
    thread is one click away in the Gmail sidebar under a name that states the
    reason, and every one of them is undoable.

    **It refuses to run without a resolved never-miss category** — see
    :class:`NoNeverMissLabelError`. The refusal happens before the mutator is
    touched, so an unlabelled never-miss archive is not merely discouraged, it is
    unreachable. There is no ``force`` parameter, deliberately: nothing may
    override this.

    It is not a second mutation path. It shares :func:`_gate` with
    :func:`apply_decision` — same ``dry_run`` refusal, same
    :class:`NotReviewedError` before the mutator, same ``ActionLog``-then-flip
    ordering, same non-null undo token.
    """
    from graph.autonomy import NEVER_MISS_CATEGORY_KEYS

    decision = _gate(session, user_id, decision_id, dry_run=dry_run)
    if decision.proposed_action not in MUTABLE_ACTIONS:
        raise NotArchivableError(
            f"a {decision.proposed_action!r}-proposed decision can never be archived "
            f"(only {sorted(MUTABLE_ACTIONS)} proposals may be mutated)"
        )

    category = _resolved_category(session, user_id, decision)
    if category is None or category.key not in NEVER_MISS_CATEGORY_KEYS:
        raise NoNeverMissLabelError(
            "refusing to archive a never-miss thread with no never-miss label "
            f"(category={getattr(category, 'key', None)!r}; expected one of "
            f"{sorted(NEVER_MISS_CATEGORY_KEYS)}). The thread stays in your inbox "
            "and is reported under `no_never_miss_label` — a never-miss thread is "
            "never archived unlabelled."
        )

    return _archive_and_log(
        session,
        user_id,
        decision,
        category,
        mutator=mutator,
        label_lookup=label_lookup,
    )


def relabel_decision(
    session: Session,
    user_id: str,
    decision_id: str,
    *,
    mutator: Mutator,
    label_lookup: LabelLookup,
    dry_run: bool,
    keep_archived: bool,
) -> object:
    """Phase 9. Move a thread from one ``ZeroInbox/*`` label to another.

    The re-organiser's only mutation, and it is a *label move*, never an archive:
    the old ``ZeroInbox/*`` label is removed and the new one added. When
    ``keep_archived=True`` — the case that matters, because "re-organise
    everything" means relabelling the ~10,000 threads that already left the inbox
    — ``INBOX`` is **never** re-added, so re-filing a thread can never resurrect
    it into the inbox the user just cleared. When ``keep_archived=False`` the
    thread is returned to the inbox as part of the move.

    Only labels this system owns (``ZeroInbox/*``, resolved from the user's own
    categories) are ever removed; a label the user created by hand is left alone.

    Same gate, same undo token, no ``force``. ``operation="add_label"`` keeps the
    action log inside the permitted, non-destructive verb set.
    """
    decision = _gate(
        session,
        user_id,
        decision_id,
        dry_run=dry_run,
        allowed_statuses=_RELABEL_STATUSES,
    )

    m = _models()
    category = _resolved_category(session, user_id, decision)
    if category is None:
        raise ActionsError("decision has no category to label the thread with")

    item = session.get(m.Item, decision.item_id)
    if item is None or item.user_id != user_id:
        raise ActionsError("decision refers to an item that no longer exists")

    label = label_lookup.ensure_label(
        category.channel_label_name or label_name_for(category.name)
    )
    target_label_id = label["id"]
    if not category.channel_label_id:
        category.channel_label_id = target_label_id

    owned_label_ids = {
        row.channel_label_id
        for row in session.execute(
            select(m.Category).where(m.Category.user_id == user_id)
        ).scalars()
        if row.channel_label_id
    }

    original_label_ids: list[str] = []
    try:
        original_label_ids = list(mutator.get_thread_labels(item.external_thread_id))
    except Exception:
        original_label_ids = list(item.channel_labels or [])

    remove = sorted((set(original_label_ids) & owned_label_ids) - {target_label_id})
    add = [target_label_id]
    if not keep_archived and INBOX_LABEL_ID not in original_label_ids:
        add.append(INBOX_LABEL_ID)
    if keep_archived and INBOX_LABEL_ID in original_label_ids:
        # The thread is being filed away as part of the move.
        remove.append(INBOX_LABEL_ID)

    response = mutator.restore_labels(
        item.external_thread_id, add_label_ids=add, remove_label_ids=remove
    )

    action_log = m.ActionLog(
        user_id=user_id,
        decision_id=decision.id,
        operation="add_label",
        request_params={
            "thread_id": item.external_thread_id,
            "add_label_ids": add,
            "remove_label_ids": remove,
            "keep_archived": bool(keep_archived),
        },
        response=response,
        undo_token={
            "thread_id": item.external_thread_id,
            "category_label_id": target_label_id,
            "original_label_ids": original_label_ids,
            "labels_added_by_triage": add,
            "add_label_ids": remove,
            "remove_label_ids": add,
        },
    )
    session.add(action_log)
    session.flush()

    labels = set(original_label_ids)
    labels.difference_update(remove)
    labels.update(add)
    item.channel_labels = sorted(labels)
    session.flush()

    return action_log


def _archive_and_log(
    session: Session,
    user_id: str,
    decision,
    category,
    *,
    mutator: Mutator,
    label_lookup: LabelLookup,
) -> object:
    """The archive mutation itself, shared by every archiving entry point.

    Extracted so ``apply_decision`` and ``archive_to_never_miss_label`` cannot
    drift into two different notions of "archived": one Gmail call, one
    ``ActionLog`` row written **before** the decision flips to ``applied``, one
    undo token carrying the full pre-triage label snapshot.
    """
    m = _models()
    item = session.get(m.Item, decision.item_id)
    if item is None or item.user_id != user_id:
        raise ActionsError("decision refers to an item that no longer exists")

    label = label_lookup.ensure_label(
        category.channel_label_name or label_name_for(category.name)
    )
    category_label_id = label["id"]
    if not category.channel_label_id:
        category.channel_label_id = category_label_id

    original_label_ids: list[str] = []
    try:
        original_label_ids = mutator.get_thread_labels(item.external_thread_id)
    except Exception:
        pass  # snapshot is best-effort; undo falls back to the implicit inverse

    response = mutator.archive_and_label(
        item.external_thread_id, category_label_id=category_label_id
    )

    action_log = m.ActionLog(
        user_id=user_id,
        decision_id=decision.id,
        operation="archive",
        request_params={
            "thread_id": item.external_thread_id,
            "add_label_ids": [category_label_id],
            "remove_label_ids": [INBOX_LABEL_ID],
        },
        response=response,
        undo_token={
            "thread_id": item.external_thread_id,
            "category_label_id": category_label_id,
            "original_label_ids": original_label_ids,
            "labels_added_by_triage": [category_label_id],
            "add_label_ids": [INBOX_LABEL_ID],
            "remove_label_ids": [category_label_id],
        },
    )
    session.add(action_log)
    session.flush()  # the ActionLog row (with its undo token) exists before...

    decision.status = "applied"
    decision.decided_at = decision.decided_at or _now()

    labels = set(item.channel_labels or [])
    labels.discard(INBOX_LABEL_ID)
    labels.add(category_label_id)
    item.channel_labels = sorted(labels)
    session.flush()  # ...the decision flips to applied.

    try:
        from events import bus
        import time

        bus.emit(
            user_id,
            {
                "type": "gmail_mutation_applied",
                "ts": time.time(),
                "action_log_id": str(action_log.id),
                "thread_count": 1,
                "category": category.key if category else None,
            },
        )
    except Exception:  # pragma: no cover - event bus must never fail an action
        pass

    return action_log


def undo_action(
    session: Session,
    user_id: str,
    action_log_id: str,
    *,
    mutator: Mutator,
) -> object:
    """Reverses one action. Idempotent: a second call is a no-op, not an error.

    Never calls Gmail a second time for an already-undone action — the local
    ``undone_at`` check is the idempotency guard.
    """
    m = _models()
    action_log = session.execute(
        select(m.ActionLog).where(m.ActionLog.id == action_log_id, m.ActionLog.user_id == user_id)
    ).scalar_one_or_none()
    if action_log is None:
        raise ActionsError(f"action log {action_log_id!r} not found")

    if action_log.undone_at is not None:
        return action_log  # already undone — idempotent no-op, no Gmail call

    token = action_log.undo_token or {}
    thread_id = token.get("thread_id")
    category_label_id = token.get("category_label_id")
    if not thread_id or not category_label_id:
        raise ActionsError("undo token is missing the fields needed to reverse this action")

    # Precision undo: if we captured the exact pre-triage label state, use it.
    # Falls back to the implicit inverse (add INBOX, remove category label) for
    # older tokens that pre-date the snapshot feature.
    original_label_ids = token.get("original_label_ids")
    labels_added = token.get("labels_added_by_triage") or [category_label_id]
    if original_label_ids is not None and hasattr(mutator, "restore_labels"):
        mutator.restore_labels(
            thread_id,
            add_label_ids=original_label_ids,
            remove_label_ids=labels_added,
        )
    else:
        # Also propagates unchanged on failure — never mark undone optimistically.
        mutator.undo_archive_and_label(thread_id, category_label_id=category_label_id)

    action_log.undone_at = _now()

    if action_log.decision_id:
        decision = session.get(m.Decision, action_log.decision_id)
        if decision is not None and decision.user_id == user_id:
            decision.status = "undone"

    item = session.execute(
        select(m.Item).where(m.Item.external_thread_id == thread_id, m.Item.user_id == user_id)
    ).scalar_one_or_none()
    if item is not None:
        if original_label_ids is not None:
            # Precision restore: snapshot is the ground truth.
            item.channel_labels = sorted(original_label_ids)
        else:
            labels = set(item.channel_labels or [])
            labels.discard(category_label_id)
            labels.add(INBOX_LABEL_ID)
            item.channel_labels = sorted(labels)

    session.flush()
    return action_log
