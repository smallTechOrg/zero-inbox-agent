"""``GET /api/categories/{id}/usage`` — what actually references a category.

Read-only, user-scoped, and it exists for one reason: **a category is never
deleted "because it looked empty"**.

The live account carries a category ``e2e-actions-test`` left behind by a test
that ran against production data. Removing it goes through the normal taxonomy
path — this endpoint, then ``DELETE /api/categories/{id}`` — and the deletion is
permitted only when every count here is zero. A non-zero count stops the
operation and is reported. See ``scripts/README_cleanup.md`` (the procedure; there
is deliberately no script, because an ad-hoc script against ``zero_inbox.db`` is
exactly how the row got there).

Response shape (fixed contract — ``scripts/README_cleanup.md`` depends on it)::

    {"data": {"category_id": ..., "key": ..., "name": ...,
              "decisions": n, "rules": n, "items": n,
              "safe_to_delete": bool}, "error": null}

* ``decisions`` — decision rows filed under this category, in any state.
* ``rules`` — rules that would file mail into it, mined or user-written. A rule
  outliving its category is a rule that can never take effect, so it is counted.
* ``items`` — distinct threads behind those decisions. ``decisions`` counts rows
  and ``items`` counts mail; re-triaging the same thread produces several
  decisions, so the two differ and reporting only one of them would understate
  what the user is about to detach.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api._common import not_found, ok
from api.session import require_user_id
from db.session import get_session

router = APIRouter()


def category_usage(session: Session, *, user_id: str, category_id: str) -> dict:
    """The true counts referencing ``category_id``. Raises ``LookupError`` if absent."""
    from db.models import Category, Decision, Rule

    category = session.get(Category, category_id)
    if category is None or category.user_id != user_id:
        raise LookupError("category not found")

    decisions = int(
        session.execute(
            select(func.count(Decision.id)).where(
                Decision.user_id == user_id, Decision.category_id == category_id
            )
        ).scalar_one()
        or 0
    )
    items = int(
        session.execute(
            select(func.count(func.distinct(Decision.item_id))).where(
                Decision.user_id == user_id, Decision.category_id == category_id
            )
        ).scalar_one()
        or 0
    )

    # Rules reference a category by KEY inside the JSON ``action`` blob
    # (``{"set_category": "receipts"}``), which no dialect can filter portably —
    # so the user's rules are loaded and folded in Python. A user has tens of
    # rules, not thousands, and a `LIKE` against serialised JSON would silently
    # match a substring of some other field.
    rules = 0
    for row in session.execute(select(Rule).where(Rule.user_id == user_id)).scalars():
        if ((row.action or {}).get("set_category") or "") == category.key:
            rules += 1

    return {
        "category_id": category.id,
        "key": category.key,
        "name": category.name,
        "decisions": decisions,
        "rules": rules,
        "items": items,
        "safe_to_delete": decisions == 0 and rules == 0 and items == 0,
    }


@router.get("/api/categories/{category_id}/usage")
def category_usage_route(
    category_id: str,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    try:
        usage = category_usage(session, user_id=user_id, category_id=category_id)
    except LookupError as exc:
        raise not_found("Category") from exc
    return ok(usage)
