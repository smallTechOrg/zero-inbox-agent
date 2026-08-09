"""Triage run progress (polled once a second by the progress bar) and cancellation."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from api._common import iso, not_found, ok
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


@router.get("/api/runs/{run_id}")
def get_run(
    run_id: str,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    return ok(run_payload(load_run(session, run_id, user_id)))


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
