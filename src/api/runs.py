"""Triage run progress (polled once a second by the progress bar) and cancellation."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api._common import DRY_RUN_VIOLATION, NOT_FOUND, PROVIDER_ERROR, VALIDATION_ERROR, api_error, iso, not_found, ok
from api.session import require_user_id
from db.session import get_session

router = APIRouter()

TERMINAL_STATUSES = ("completed", "failed", "cancelled")


def run_payload(run) -> dict:
    return {
        "id": run.id,
        "status": run.status,
        "dry_run": bool(run.dry_run),
        "items_total": run.items_total or 0,
        "items_decided": run.items_decided or 0,
        "counts": run.counts or {},
        "cost": {
            "tokens_in": run.tokens_in or 0,
            "tokens_out": run.tokens_out or 0,
            "usd": run.cost_usd or 0.0,
        },
        "error_message": run.error_message,
        "started_at": iso(run.started_at),
        "finished_at": iso(run.finished_at),
    }


def load_run(session: Session, run_id: str, user_id: str):
    from db.models import TriageRun

    run = session.get(TriageRun, run_id)
    if run is None or run.user_id != user_id:
        raise not_found("Run")
    return run


@router.get("/api/runs/latest")
def get_latest_run(
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    """The run worth showing on dashboard load — completed or running only.

    Registered ahead of ``/api/runs/{run_id}`` so the literal path ``latest``
    is matched here rather than treated as a run id (Starlette matches routes
    in registration order, not by specificity).
    """
    from api.triage import _latest_run_id

    run_id = _latest_run_id(session, user_id)
    if run_id is None:
        return ok(None)
    return ok(run_payload(load_run(session, run_id, user_id)))


@router.get("/api/runs/{run_id}")
def get_run(
    run_id: str,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    return ok(run_payload(load_run(session, run_id, user_id)))


@router.get("/api/runs/{run_id}/summary")
def get_run_summary(
    run_id: str,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    from db.models import Cluster, Decision, TriageRun

    run = load_run(session, run_id, user_id)

    # Categories: group decisions by category name via join
    from db.models import Category

    cat_rows = session.execute(
        select(Category.name, Decision.proposed_action, func.count(Decision.id).label("cnt"))
        .join(Category, Decision.category_id == Category.id)
        .where(Decision.run_id == run_id, Decision.user_id == user_id)
        .group_by(Category.name, Decision.proposed_action)
    ).all()

    # Aggregate categories: pick the dominant suggested_action per name
    cat_map: dict[str, dict] = {}
    for name, action, cnt in cat_rows:
        if name not in cat_map:
            cat_map[name] = {"name": name, "count": 0, "suggested_action": action}
        cat_map[name]["count"] += cnt
        # keep highest-count action per category
        if cnt > cat_map[name]["count"]:
            cat_map[name]["suggested_action"] = action

    categories = list(cat_map.values())

    # Top 3 clusters by item_count
    top_cluster_rows = session.execute(
        select(Cluster.label, Cluster.item_count, Cluster.suggested_action)
        .where(Cluster.run_id == run_id, Cluster.user_id == user_id)
        .order_by(Cluster.item_count.desc())
        .limit(3)
    ).all()

    top_clusters = [
        {"label": label, "count": count, "suggested_action": action}
        for label, count, action in top_cluster_rows
    ]

    needs_your_call_count = session.execute(
        select(func.count(Decision.id)).where(
            Decision.run_id == run_id,
            Decision.user_id == user_id,
            Decision.status == "needs_your_call",
        )
    ).scalar_one()

    return ok(
        {
            "run_id": run_id,
            "status": run.status,
            "total_threads": run.items_total or 0,
            "categories": categories,
            "top_clusters": top_clusters,
            "needs_your_call_count": needs_your_call_count or 0,
            "cost_usd": run.cost_usd or 0.0,
            "completed_at": iso(run.finished_at),
        }
    )


@router.post("/api/runs/{run_id}/approve-and-apply")
def approve_and_apply(
    run_id: str,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    """Bulk-approve all non-needs_your_call, non-keep decisions and apply them."""
    from db.models import Decision, UserSettings

    load_run(session, run_id, user_id)  # ownership check

    # Check dry_run setting
    settings = session.get(UserSettings, user_id)
    if settings is None or bool(settings.dry_run):
        raise api_error(DRY_RUN_VIOLATION, "dry_run is on — no mutation was attempted", 409)

    # Load eligible decisions: not needs_your_call
    decisions = session.execute(
        select(Decision).where(
            Decision.run_id == run_id,
            Decision.user_id == user_id,
            Decision.status != "needs_your_call",
        )
    ).scalars().all()

    skipped_needs_your_call = 0
    skipped_keep = 0
    approved_ids: list[str] = []

    for d in decisions:
        if d.proposed_action == "keep":
            skipped_keep += 1
            continue
        d.status = "approved"
        approved_ids.append(d.id)

    session.flush()

    # Apply each approved decision
    from api.actions import _mutator_and_labels_for_user
    from channels.base import ChannelError, DryRunViolation
    from tools.actions import ActionsError, NeedsYourCallError, NotApprovedError, apply_decision

    mutator, label_lookup = _mutator_and_labels_for_user(session, user_id)

    applied = 0
    undo_tokens: list[dict] = []

    for decision_id in approved_ids:
        try:
            action_log = apply_decision(
                session,
                user_id,
                decision_id,
                mutator=mutator,
                label_lookup=label_lookup,
                dry_run=False,
            )
            session.commit()
            applied += 1
            if action_log.undo_token:
                undo_tokens.append({"action_log_id": action_log.id, "undo_token": action_log.undo_token})
        except (NeedsYourCallError, NotApprovedError, ActionsError, ChannelError, DryRunViolation):
            session.rollback()
            # Continue with other decisions

    return ok(
        {
            "applied": applied,
            "skipped_keep": skipped_keep,
            "skipped_needs_your_call": skipped_needs_your_call,
            "undo_tokens": undo_tokens,
        }
    )


@router.post("/api/runs/{run_id}/cancel")
def cancel_run(
    run_id: str,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    run = load_run(session, run_id, user_id)
    # Cancellation is a persisted flag: the graph checks the run row between batches.
    # Cancelling an already-finished run is a no-op that reports its real status.
    if run.status not in TERMINAL_STATUSES:
        run.status = "cancelled"
        run.finished_at = datetime.now(timezone.utc)
    return ok({"status": run.status})
