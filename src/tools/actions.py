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


class Mutator(Protocol):
    def archive_and_label(self, thread_id: str, *, category_label_id: str) -> dict: ...
    def undo_archive_and_label(self, thread_id: str, *, category_label_id: str) -> dict: ...


class LabelLookup(Protocol):
    def ensure_label(self, name: str) -> dict: ...


def _models():
    from db import models

    return models


def _now() -> datetime:
    return datetime.now(timezone.utc)


def apply_decision(
    session: Session,
    user_id: str,
    decision_id: str,
    *,
    mutator: Mutator,
    label_lookup: LabelLookup,
    dry_run: bool,
) -> object:
    """Applies one ``approved`` decision. Returns the created ``ActionLog`` row.

    Writes the ``ActionLog`` row (with its undo token) *before* flipping the decision
    to ``applied`` — the ordering the spec requires. Raises ``DryRunViolation``,
    :class:`NeedsYourCallError`, :class:`NotApprovedError` or :class:`ActionsError`
    without ever calling ``mutator`` if the decision is not eligible.
    """
    if dry_run:
        raise DryRunViolation("dry_run is on — no mutation was attempted")

    m = _models()
    decision = session.execute(
        select(m.Decision).where(m.Decision.id == decision_id, m.Decision.user_id == user_id)
    ).scalar_one_or_none()
    if decision is None:
        raise ActionsError(f"decision {decision_id!r} not found")
    if decision.status == "needs_your_call":
        raise NeedsYourCallError("a decision in needs_your_call can never be mutated")
    if decision.status != "approved":
        raise NotApprovedError(
            f"only approved decisions are eligible for apply (status={decision.status!r})"
        )
    if decision.proposed_action not in ("archive", "digest"):
        raise NotArchivableError(
            f"a {decision.proposed_action!r}-proposed decision can never be archived "
            "(only 'archive' and 'digest' proposals may be mutated)"
        )

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

    # The single atomic Gmail call. Failure here (after 3 retries inside the
    # mutator) propagates unchanged — nothing below is executed, nothing is
    # logged as applied, and the decision stays `approved` for Retry.
    response = mutator.archive_and_label(item.external_thread_id, category_label_id=category_label_id)

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
        labels = set(item.channel_labels or [])
        labels.discard(category_label_id)
        labels.add(INBOX_LABEL_ID)
        item.channel_labels = sorted(labels)

    session.flush()
    return action_log
