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



@router.post("/api/runs/{run_id}/undo")
def undo_run(
    run_id: str,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    """Reverse all Gmail mutations from this run in reverse chronological order.

    Idempotent: a second call on an already-undone run returns the same result.
    Only ``completed`` runs may be undone — not running or cancelled runs.
    """
    from db.models import ActionLog, Decision

    run = load_run(session, run_id, user_id)
    if run.status not in ("completed",):
        raise api_error(
            VALIDATION_ERROR,
            f"can only undo a completed run (status={run.status!r})",
            422,
        )

    # Load all action logs for this run's decisions, reverse-chronological.
    action_logs = session.execute(
        select(ActionLog)
        .join(Decision, ActionLog.decision_id == Decision.id)
        .where(
            ActionLog.user_id == user_id,
            Decision.run_id == run_id,
        )
        .order_by(ActionLog.created_at.desc())
    ).scalars().all()

    if not action_logs:
        return ok({"reversed": 0, "skipped": 0, "errors": []})

    from api.actions import _mutator_and_labels_for_user
    from channels.base import ChannelError

    try:
        mutator, _ = _mutator_and_labels_for_user(session, user_id)
    except Exception as exc:
        raise api_error(PROVIDER_ERROR, f"could not build Gmail client: {exc}", 502) from exc

    reversed_count = 0
    skipped_count = 0
    errors: list[str] = []

    for action_log in action_logs:
        if action_log.undone_at is not None:
            # Already undone — idempotent skip.
            skipped_count += 1
            continue
        try:
            token = action_log.undo_token or {}
            thread_id = token.get("thread_id")
            if not thread_id:
                errors.append(f"action_log {action_log.id}: missing thread_id in undo_token")
                continue

            original_label_ids = token.get("original_label_ids")
            labels_added = token.get("labels_added_by_triage") or [token.get("category_label_id")]
            labels_added = [lb for lb in labels_added if lb]

            if original_label_ids is not None:
                mutator.restore_labels(
                    thread_id,
                    add_label_ids=original_label_ids,
                    remove_label_ids=labels_added,
                )
            else:
                # Legacy token — fall back to implicit inverse.
                category_label_id = token.get("category_label_id")
                if not category_label_id:
                    errors.append(f"action_log {action_log.id}: undo_token lacks category_label_id")
                    continue
                mutator.undo_archive_and_label(thread_id, category_label_id=category_label_id)

            from datetime import datetime, timezone

            action_log.undone_at = datetime.now(timezone.utc)
            if action_log.decision_id:
                decision = session.get(Decision, action_log.decision_id)
                if decision is not None and decision.user_id == user_id:
                    decision.status = "undone"
            session.flush()
            reversed_count += 1
        except ChannelError as exc:
            errors.append(f"action_log {action_log.id}: {exc}")
        except Exception as exc:
            errors.append(f"action_log {action_log.id}: {exc}")

    return ok({"reversed": reversed_count, "skipped": skipped_count, "errors": errors})


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
