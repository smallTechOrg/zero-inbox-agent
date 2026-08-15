"""``/api/reorg`` — start, watch, cancel and bulk-undo a re-organisation.

spec/api.md § Phase 9. Every route is user-scoped and ``404``s for another user's
job: a job id is not a capability.

This module exports ``router`` and does **not** mount it — ``api/__init__.py`` is
owned by slice 4, which mounts both new routers.
"""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api._common import PROVIDER_ERROR, api_error, iso, not_found, ok
from api.session import require_user_id
from db.session import get_session
from jobs import reorganise

_log = logging.getLogger("zero_inbox.api.reorg")

router = APIRouter()

#: spec/api.md § Error codes — 409, a re-organisation is already running.
REORG_IN_PROGRESS = "reorg_in_progress"

#: spec/api.md § Error codes — 409, a triage run is applying. Two writers against
#: one mailbox is not a state this system is specified to support.
RUN_IN_PROGRESS = "run_in_progress"

#: Job ids whose worker is in flight in this process. A second POST while one is
#: running is refused by the database check below; this guards the narrower race
#: of two workers being scheduled for the same row.
_in_flight: set[str] = set()
_lock = threading.Lock()


class ReorgRequest(BaseModel):
    #: Absolute when true: the job re-resolves every thread and produces the full
    #: ledger, and performs zero Gmail mutations.
    dry_run: bool = False


def _ledger_payload(session: Session, *, job_id: str) -> dict:
    payload = reorganise.ledger(session, job_id=job_id)
    payload["started_at"] = iso(payload.get("started_at"))
    payload["finished_at"] = iso(payload.get("finished_at"))
    return payload


def _load_job(session: Session, job_id: str, user_id: str):
    from db.models import ReorgJob

    job = session.get(ReorgJob, job_id)
    if job is None or job.user_id != user_id:
        raise not_found("Re-organisation")
    return job


def _worker(*, job_id: str, user_id: str) -> None:
    """Run the job outside the request. Never raises into the server."""
    try:
        reorganise.execute(job_id, user_id=user_id)
    except Exception:  # pragma: no cover - execute() already swallows and records
        _log.warning("reorg.worker_failed job_id=%s", job_id, exc_info=True)
    finally:
        with _lock:
            _in_flight.discard(job_id)


@router.post("/api/reorg")
def start_reorg(
    body: ReorgRequest | None = None,
    background_tasks: BackgroundTasks = None,  # type: ignore[assignment]
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    """Re-organise **every** past decision, including already-archived threads.

    ``409 reorg_in_progress`` if one is already running for this user;
    ``409 run_in_progress`` if a triage run is in flight. The response is the
    ledger as it stands the instant the job is created — ``done`` and the
    skipped-by-reason table stream from there via ``GET /api/reorg/{job_id}`` and
    the ``reorg_progress`` SSE events.
    """
    dry_run = bool(body.dry_run) if body is not None else False
    try:
        job_id = reorganise.start(session, user_id=user_id, dry_run=dry_run)
    except reorganise.ReorgInProgress as exc:
        raise api_error(REORG_IN_PROGRESS, str(exc), 409) from exc
    except reorganise.RunInProgress as exc:
        raise api_error(RUN_IN_PROGRESS, str(exc), 409) from exc
    session.commit()

    with _lock:
        queued = job_id not in _in_flight
        if queued:
            _in_flight.add(job_id)
    if queued and background_tasks is not None:
        background_tasks.add_task(_worker, job_id=job_id, user_id=user_id)

    payload = _ledger_payload(session, job_id=job_id)
    payload["queued"] = queued
    return ok(payload)


@router.get("/api/reorg/{job_id}")
def get_reorg(
    job_id: str,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    """The live ledger. ``done + sum(skipped.values()) == total`` always."""
    _load_job(session, job_id, user_id)
    return ok(_ledger_payload(session, job_id=job_id))


@router.post("/api/reorg/{job_id}/cancel")
def cancel_reorg(
    job_id: str,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    """Stop the job. Already-done work stays done and is fully undoable."""
    _load_job(session, job_id, user_id)
    payload = reorganise.cancel(session, job_id=job_id, user_id=user_id)
    session.commit()
    return ok(
        {
            **payload,
            "started_at": iso(payload.get("started_at")),
            "finished_at": iso(payload.get("finished_at")),
        }
    )


@router.post("/api/reorg/{job_id}/undo")
def undo_reorg(
    job_id: str,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    """Undo the whole re-organisation — one button, one operation, idempotent.

    A second call performs **zero** Gmail calls and returns the same counts.
    """
    _load_job(session, job_id, user_id)

    from api.actions import _mutator_and_labels_for_user

    try:
        mutator, _labels = _mutator_and_labels_for_user(session, user_id)
    except Exception as exc:
        raise api_error(PROVIDER_ERROR, f"could not build Gmail client: {exc}", 502) from exc

    result = reorganise.undo(session, job_id=job_id, user_id=user_id, mutator=mutator)
    session.commit()
    return ok(result)
