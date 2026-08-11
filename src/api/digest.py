"""Catch-up digest for the most recent completed run.

GET /api/digest/latest  — returns a structured summary of what the agent did.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from api._common import iso, not_found, ok
from api.session import require_user_id
from db.session import get_session

router = APIRouter()


@router.get("/api/digest/latest")
def get_latest_digest(
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    from db.models import Category, Decision, Item, TriageRun

    # Find the most recent completed run for this user
    run = session.execute(
        select(TriageRun)
        .where(TriageRun.user_id == user_id, TriageRun.status == "completed")
        .order_by(TriageRun.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    if run is None:
        raise not_found("completed run")

    run_id = run.id

    # Load all decisions with their items for this run
    rows = session.execute(
        select(Decision, Item)
        .join(Item, Decision.item_id == Item.id)
        .where(Decision.run_id == run_id, Decision.user_id == user_id)
    ).all()

    time_sensitive_kept = []
    vip_mail = []
    needs_your_call = []
    auto_archived_by_cat: dict[str, int] = {}
    auto_archived_count = 0

    for decision, item in rows:
        subject = item.subject or ""
        from_addr = item.from_email or item.from_name or ""

        if decision.time_sensitive and decision.proposed_action == "keep":
            time_sensitive_kept.append(
                {"subject": subject, "from": from_addr, "reason": decision.reasoning or ""}
            )

        if decision.decided_by and ("vip" in decision.decided_by or "never_miss" in decision.decided_by):
            vip_mail.append(
                {"subject": subject, "from": from_addr, "reason": decision.reasoning or ""}
            )

        if decision.status == "needs_your_call":
            needs_your_call.append(
                {"subject": subject, "from": from_addr, "reasoning": decision.reasoning or ""}
            )

        if decision.status == "applied" or (
            decision.proposed_action == "archive" and decision.status == "approved"
        ):
            auto_archived_count += 1
            # Get category name
            if decision.category_id:
                cat = session.get(Category, decision.category_id)
                cat_name = cat.name if cat else "uncategorized"
            else:
                cat_name = "uncategorized"
            auto_archived_by_cat[cat_name] = auto_archived_by_cat.get(cat_name, 0) + 1

    by_category = [
        {"name": name, "count": count} for name, count in auto_archived_by_cat.items()
    ]

    return ok(
        {
            "run_id": run_id,
            "generated_at": iso(run.finished_at),
            "time_sensitive_kept": time_sensitive_kept,
            "vip_mail": vip_mail,
            "needs_your_call": needs_your_call,
            "auto_archived": {"count": auto_archived_count, "by_category": by_category},
        }
    )
