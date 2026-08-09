"""Launching a triage run for a connected mailbox.

``POST /api/connections/{connection_id}/triage`` creates the ``TriageRun`` row up front
(status ``running``) so the client has something to poll immediately, then runs the
LangGraph triage graph as an in-process background task. All progress is persisted on the
run row by the graph — the API holds no in-memory run state, so progress survives a reload
and the run is cancellable purely through the database.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api._common import REAUTH_REQUIRED, api_error, not_found, ok
from api.session import require_user_id
from db.session import get_session

router = APIRouter()

MAX_LIMIT = 500
DEFAULT_LIMIT = 200


class TriageRequest(BaseModel):
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)


@router.post("/api/connections/{connection_id}/triage")
def start_triage(
    connection_id: str,
    background_tasks: BackgroundTasks,
    req: TriageRequest | None = None,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    from db.models import ChannelAccount, TriageRun

    limit = (req or TriageRequest()).limit

    account = session.get(ChannelAccount, connection_id)
    # Scope check and existence check are the same check: another user's connection
    # simply does not exist as far as this session is concerned.
    if account is None or account.user_id != user_id:
        raise not_found("Connection")
    if account.status == "reauth_required":
        raise api_error(REAUTH_REQUIRED, "Reconnect this Gmail account to run triage.")
    if account.status == "revoked":
        raise api_error(REAUTH_REQUIRED, "This Gmail connection was revoked. Reconnect it.")

    run = TriageRun(
        id=str(uuid4()),
        user_id=user_id,
        channel_account_id=account.id,
        kind="incremental",
        status="running",
        dry_run=True,  # Phase 1 forces dry-run server-side; the client cannot turn it off.
        items_total=0,
        items_decided=0,
        counts={},
        started_at=datetime.now(timezone.utc),
    )
    session.add(run)
    # Commit before returning, not at dependency teardown: teardown happens after
    # the response is sent, so the client could poll GET /api/runs/{id} — and the
    # background task could open its own session — before the row was durable,
    # and both would see a 404 for a run that was just created.
    session.commit()
    run_id = run.id

    background_tasks.add_task(
        _run_triage_task,
        run_id=run_id,
        user_id=user_id,
        channel_account_id=account.id,
        limit=limit,
    )
    return ok({"run_id": run_id})


def _run_triage_task(*, run_id: str, user_id: str, channel_account_id: str, limit: int) -> None:
    """Background worker. Never raises — a failure is recorded on the run row."""
    from db.session import create_db_session

    try:
        from graph.runner import run_triage

        run_triage(
            user_id=user_id,
            channel_account_id=channel_account_id,
            limit=limit,
            dry_run=True,
            run_id=run_id,
        )
    except Exception as exc:  # noqa: BLE001 — a crashed run must still be visible to the UI
        from db.models import TriageRun

        with create_db_session() as session:
            run = session.get(TriageRun, run_id)
            if run is not None and run.status == "running":
                run.status = "failed"
                run.error_message = f"{type(exc).__name__}: {exc}"
                run.finished_at = datetime.now(timezone.utc)
