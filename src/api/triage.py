"""The clustered triage queue: clusters and read-only thread history.

All decisions are now applied automatically after a run completes (autonomous mode).
The approve/reject and review-all endpoints have been removed — see spec/api.md.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from api._common import iso, not_found, ok
from api.session import require_user_id
from db.session import get_session

router = APIRouter()

SAMPLE_SUBJECT_COUNT = 3


_DEFAULT_VIEW_STATUSES = ("completed", "running")


def _latest_run_id(session: Session, user_id: str) -> str | None:
    """The run worth showing by default.

    Restricted to ``completed``/``running`` — a cancelled or failed run must
    never silently become the default view and bury a prior good completed
    run. A user actively watching a run in progress still sees it live.
    """
    from db.models import TriageRun

    return session.execute(
        select(TriageRun.id)
        .where(
            TriageRun.user_id == user_id,
            TriageRun.status.in_(_DEFAULT_VIEW_STATUSES),
        )
        .order_by(TriageRun.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _resolve_run_id(session: Session, user_id: str, run_id: str | None) -> str | None:
    return run_id if run_id else _latest_run_id(session, user_id)


@router.get("/api/triage/clusters")
def list_clusters(
    run_id: str | None = Query(default=None),
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    from db.models import Cluster, Decision, Item

    effective_run_id = _resolve_run_id(session, user_id, run_id)
    if effective_run_id is None:
        return ok([])

    clusters = session.execute(
        select(Cluster)
        .where(Cluster.user_id == user_id, Cluster.run_id == effective_run_id)
        .order_by(Cluster.item_count.desc())
    ).scalars().all()

    payload = []
    for cluster in clusters:
        subjects = session.execute(
            select(Item.subject)
            .join(Decision, Decision.item_id == Item.id)
            .where(Decision.user_id == user_id, Decision.cluster_id == cluster.id)
            .order_by(Item.internal_date.desc())
            .limit(SAMPLE_SUBJECT_COUNT)
        ).scalars().all()
        payload.append(
            {
                "id": cluster.id,
                "kind": cluster.kind,
                "label": cluster.label,
                "item_count": cluster.item_count,
                "suggested_action": cluster.suggested_action,
                "min_confidence": cluster.min_confidence,
                "avg_confidence": cluster.avg_confidence,
                "sample_subjects": [s for s in subjects],
            }
        )
    return ok(payload)


@router.get("/api/triage/needs-your-call")
def needs_your_call(
    run_id: str | None = Query(default=None),
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    """The below-the-floor queue: decisions the agent declined to decide on its own."""
    from db.models import Category, Decision, Item, Rule

    effective_run_id = _resolve_run_id(session, user_id, run_id)
    if effective_run_id is None:
        return ok([])

    rows = session.execute(
        select(Decision, Item, Category, Rule)
        .join(Item, Item.id == Decision.item_id)
        .outerjoin(Category, Category.id == Decision.category_id)
        .outerjoin(Rule, Rule.id == Decision.rule_id)
        .where(
            Decision.user_id == user_id,
            Decision.run_id == effective_run_id,
            Decision.status == "needs_your_call",
        )
        .order_by(Item.internal_date.desc())
    ).all()

    return ok([_decision_payload(d, i, c, r) for d, i, c, r in rows])


@router.get("/api/triage/items")
def list_items(
    cluster_id: str | None = Query(default=None),
    run_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    from db.models import Category, Decision, Item, Rule

    stmt = (
        select(Decision, Item, Category, Rule)
        .join(Item, Item.id == Decision.item_id)
        .outerjoin(Category, Category.id == Decision.category_id)
        .outerjoin(Rule, Rule.id == Decision.rule_id)
        .where(Decision.user_id == user_id)
    )

    if cluster_id:
        from db.models import Cluster

        cluster = session.get(Cluster, cluster_id)
        if cluster is None or cluster.user_id != user_id:
            raise not_found("Cluster")
        stmt = stmt.where(Decision.cluster_id == cluster_id)
    else:
        effective_run_id = _resolve_run_id(session, user_id, run_id)
        if effective_run_id is None:
            return ok([])
        stmt = stmt.where(Decision.run_id == effective_run_id)

    if status:
        stmt = stmt.where(Decision.status == status)

    rows = session.execute(stmt.order_by(Item.internal_date.desc())).all()
    return ok([_decision_payload(d, i, c, r) for d, i, c, r in rows])


def _decision_payload(decision, item, category, rule) -> dict:
    return {
        "decision_id": decision.id,
        "cluster_id": decision.cluster_id,
        "item": {
            "id": item.id,
            "subject": item.subject,
            "from_name": item.from_name,
            "from_email": item.from_email,
            "snippet_redacted": item.snippet_redacted,
            "internal_date": iso(item.internal_date),
            "message_count": item.message_count,
            "is_unread": bool(item.is_unread),
        },
        "category": category.name if category is not None else None,
        "proposed_action": decision.proposed_action,
        "confidence": decision.confidence,
        "reasoning": decision.reasoning,
        "decided_by": decision.decided_by,
        "rule_id": decision.rule_id,
        "rule_name": rule.name if rule is not None else None,
        "time_sensitive": bool(decision.time_sensitive),
        "status": decision.status,
    }


@router.get("/api/categories")
def list_categories(
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    from db.models import Category

    categories = session.execute(
        select(Category)
        .where(Category.user_id == user_id)
        .order_by(Category.sort_order, Category.name)
    ).scalars().all()
    return ok(
        [
            {
                "id": c.id,
                "key": c.key,
                "name": c.name,
                "description": c.description,
                "channel_label_name": c.channel_label_name,
                "channel_label_id": c.channel_label_id,
                "default_action": c.default_action,
                "is_default": bool(c.is_default),
                "sort_order": c.sort_order,
            }
            for c in categories
        ]
    )
