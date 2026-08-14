"""``GET /api/provider-health`` — LLM provider health for the signed-in user's run.

Reports the run's **current** model (which after a fallback is not the configured
default), the ordered fallback chain and the position in it, the circuit/degraded
counters, and the process-wide throttle state (spec/api.md § Phase 6).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from api._common import ok
from api.session import require_user_id
from db.session import get_session
from llm import health

router = APIRouter()

# A run in one of these states is the one the user is watching right now.
_ACTIVE_STATUSES = ("running", "resumable", "queued", "pending")


def _latest_run_id(db: Session, user_id: str) -> str | None:
    from db.models import TriageRun

    stmt = (
        select(TriageRun.id)
        .where(TriageRun.user_id == user_id)
        .order_by(TriageRun.started_at.desc(), TriageRun.id.desc())
        .limit(1)
    )
    active = db.execute(
        stmt.where(TriageRun.status.in_(_ACTIVE_STATUSES))  # type: ignore[arg-type]
    ).scalar_one_or_none()
    return active or db.execute(stmt).scalar_one_or_none()


@router.get("/api/provider-health")
def get_provider_health(
    run_id: str | None = Query(default=None),
    user_id: str = Depends(require_user_id),
    db: Session = Depends(get_session),
) -> dict:
    """Health for *run_id*, or for this user's most recent run when omitted.

    Auth-scoped: a run id belonging to another user is never reported on — the
    response falls back to this user's own latest run.
    """
    from db.models import TriageRun

    resolved: str | None = None
    if run_id:
        owned = db.execute(
            select(TriageRun.id).where(
                TriageRun.id == run_id, TriageRun.user_id == user_id
            )
        ).scalar_one_or_none()
        resolved = owned
    if resolved is None:
        resolved = _latest_run_id(db, user_id)

    return ok(health.snapshot(resolved))
