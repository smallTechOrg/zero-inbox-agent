"""Runs API (spec/api.md § Runs, Feed, Undo).

* ``POST /api/runs``  — manual trigger only: start, or RESUME the interrupted,
  cleaning run; the triage graph executes as a background task via
  ``graph.runner.run_triage`` (spec/agent.md contract).
* ``GET /api/runs``   — run cards, newest first (status, per-category counts,
  cost card, undo state).
* ``GET /api/runs/{id}`` — card detail + this run's decisions.
* ``POST /api/runs/{id}/undo`` — whole-run undo (``tools.undo``), streaming
  ``undo_*`` events on the run's SSE channel.

Errors are structured envelopes; any Google token failure is the
``gmail_reconnect`` state — never a traceback. Every query filters by the
session user.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from api._common import CONFLICT, api_error, gmail_reconnect, iso, not_found, ok
from channels.base import ReauthRequired
from db.models import Category, GmailAccount, Mutation, Run, ThreadDecision
from db.session import get_session
from domain.enums import GmailAccountStatus, RunStatus

_log = logging.getLogger("zero_inbox.api.runs")

router = APIRouter()

DEFAULT_CHUNK_LIMIT = 50

#: Run ids with an undo currently executing in this process — a double-click
#: must not race two undo passes over the same audit rows.
_undo_in_flight: set[str] = set()
_undo_lock = threading.Lock()


def require_current_user(request: Request, response: Response) -> str:
    """Session dependency — delegates to ``api.session`` (auth slice contract).

    Kept as an indirection owned by this slice so these routers import even
    while the auth slice is in flight, and so tests can override one dependency
    for the whole runs/audit/events surface.
    """
    from api.session import require_user_id

    try:
        return require_user_id(request, response)
    except TypeError:
        return require_user_id(request)  # tolerate a (request)-only signature


class TriggerRunBody(BaseModel):
    chunk_limit: int = Field(default=DEFAULT_CHUNK_LIMIT, ge=1, le=200)


# ---------------------------------------------------------------------------
# Gmail plumbing (undo path)
# ---------------------------------------------------------------------------


def _connected_account(session: Session, user_id: str) -> GmailAccount:
    account = session.execute(
        select(GmailAccount).where(GmailAccount.user_id == user_id)
    ).scalar_one_or_none()
    if account is None or account.status != GmailAccountStatus.CONNECTED:
        raise gmail_reconnect()
    return account


def _mark_needs_reconnect(session: Session, user_id: str) -> None:
    session.execute(
        update(GmailAccount)
        .where(GmailAccount.user_id == user_id)
        .values(status=GmailAccountStatus.NEEDS_RECONNECT)
    )
    session.commit()


def gmail_mutator_for_user(session: Session, user_id: str):
    """A :class:`GmailMutator` for the user's mailbox (undo needs no audit_writer —
    its durable record is the ``undone_at`` stamps + persisted ``undo_*`` events)."""
    from channels.gmail.adapter import _service_factory_for_refresh_token
    from channels.gmail.mutations import GmailMutator, gmail_writes_disabled
    from channels.gmail.oauth import google_oauth_config
    from security.crypto import TokenCipher

    account = _connected_account(session, user_id)
    if gmail_writes_disabled():
        # Test-isolation guard: every apply() is a recorded no-op, so no
        # credentials (and no Gmail service) are needed — or allowed.
        return GmailMutator(None)
    try:
        refresh_token = TokenCipher().decrypt(account.refresh_token_encrypted)
        service = _service_factory_for_refresh_token(google_oauth_config(), refresh_token)()
    except Exception as exc:  # noqa: BLE001 — undecryptable/undecodable token == reconnect
        _log.warning("runs.gmail_client_failed user_id=%s error=%r", user_id, exc)
        raise gmail_reconnect() from exc
    return GmailMutator(service)


def label_lookup_for_user(session: Session, user_id: str) -> dict[str, str]:
    """``label name -> gmail_label_id`` from the user's categories (lazily created
    label ids are written back onto categories by the triage graph).

    Keyed by BOTH the bare category name and the namespaced Gmail label name
    (``ZI/<Category>``) — audit rows store the latter. Under the test-isolation
    guard, labels are never really created, so known categories resolve to a
    simulated id (the inverse is a recorded no-op anyway); an audit row naming
    NO known category still fails to resolve, exactly as on live Gmail."""
    from channels.gmail.labels import label_name_for
    from channels.gmail.mutations import gmail_writes_disabled

    rows = session.execute(
        select(Category.name, Category.gmail_label_id).where(Category.user_id == user_id)
    ).all()
    lookup: dict[str, str] = {}
    for name, label_id in rows:
        if not label_id and gmail_writes_disabled():
            label_id = f"simulated-{name}"
        if label_id:
            lookup[name] = label_id
            lookup[label_name_for(name)] = label_id
    return lookup


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _live_category_counts(session: Session, run: Run) -> dict[str, int]:
    rows = session.execute(
        select(Category.name, func.count(ThreadDecision.id))
        .join(Category, ThreadDecision.category_id == Category.id)
        .where(
            ThreadDecision.run_id == run.id,
            ThreadDecision.user_id == run.user_id,
        )
        .group_by(Category.name)
    ).all()
    return {name: int(count) for name, count in rows}


def run_card(session: Session, run: Run) -> dict:
    """One run card (spec/ui.md): status, per-category counts, cost, undo state."""
    counts = _live_category_counts(session, run)
    if not counts:
        counts = dict(run.counts_json or {})
    pending_undo = int(
        session.execute(
            select(func.count(Mutation.id)).where(
                Mutation.run_id == run.id,
                Mutation.user_id == run.user_id,
                Mutation.undone_at.is_(None),
            )
        ).scalar_one()
        or 0
    )
    undone = run.status == RunStatus.UNDONE
    return {
        "id": run.id,
        "status": run.status,
        "trigger": run.trigger,
        "chunk_limit": run.chunk_limit,
        "started_at": iso(run.started_at),
        "finished_at": iso(run.finished_at),
        "threads_decided": run.threads_decided or 0,
        "counts": counts,
        "cost": {
            "llm_calls": run.llm_calls or 0,
            "tokens_in": run.tokens_in or 0,
            "tokens_out": run.tokens_out or 0,
            "est_cost_usd": run.est_cost_usd or 0.0,
            "fallback_events": run.fallback_events or 0,
        },
        "interrupt_reason": run.interrupt_reason,
        "undo": {
            "undone": undone,
            "undone_at": iso(run.undone_at),
            "undoable": (not undone)
            and run.status != RunStatus.RUNNING
            and pending_undo > 0,
        },
    }


def load_run(session: Session, run_id: str, user_id: str) -> Run:
    run = session.get(Run, run_id)
    if run is None or run.user_id != user_id:
        raise not_found("Run")
    return run


# ---------------------------------------------------------------------------
# Background execution (spec/agent.md: runner contract)
# ---------------------------------------------------------------------------


def _start_run_task(*, run_id: str, user_id: str) -> None:
    """Invoke the triage graph runner. Never raises into the server."""
    try:
        from graph.runner import run_triage

        try:
            run_triage(user_id=user_id, run_id=run_id)
        except TypeError:
            run_triage(user_id, run_id)  # positional-contract fallback
    except Exception as exc:  # noqa: BLE001 — belt & braces; runner owns interruption
        _log.warning("runs.background_failed run_id=%s error=%r", run_id, exc)
        try:
            from db.session import create_db_session

            with create_db_session() as session:
                run = session.get(Run, run_id)
                if run is not None and run.status == RunStatus.RUNNING:
                    run.status = RunStatus.INTERRUPTED
                    run.interrupt_reason = (
                        "The run stopped unexpectedly. Everything already decided is "
                        "saved — press Clean my inbox to resume where it stopped."
                    )
                    run.finished_at = datetime.now(timezone.utc)
        except Exception:  # pragma: no cover — reconciliation also runs at startup
            _log.warning("runs.interrupt_mark_failed run_id=%s", run_id, exc_info=True)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/api/runs")
def trigger_run(
    background_tasks: BackgroundTasks,
    body: TriggerRunBody | None = None,
    user_id: str = Depends(require_current_user),
    session: Session = Depends(get_session),
) -> dict:
    """Start — or resume the interrupted — cleaning run. Manual trigger only.

    409 (``conflict``) with the active run id if one is already running; a run
    left ``interrupted`` is put back to ``running`` (same row — data-driven
    resume: the graph's ``load_chunk`` skips every already-decided thread).
    """
    chunk_limit = (body or TriggerRunBody()).chunk_limit

    active_id = session.execute(
        select(Run.id)
        .where(Run.user_id == user_id, Run.status == RunStatus.RUNNING)
        .order_by(Run.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if active_id is not None:
        raise api_error(
            CONFLICT,
            f"A cleaning run is already in progress (run_id={active_id}).",
            409,
        )

    _connected_account(session, user_id)  # gmail_reconnect before any run row

    resumed = False
    run_id = session.execute(
        select(Run.id)
        .where(Run.user_id == user_id, Run.status == RunStatus.INTERRUPTED)
        .order_by(Run.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    if run_id is not None:
        # Atomic interrupted -> running: the database arbitrates concurrent
        # triggers; only the request whose UPDATE matches starts a worker.
        won = session.execute(
            update(Run)
            .where(
                Run.id == run_id,
                Run.user_id == user_id,
                Run.status == RunStatus.INTERRUPTED,
            )
            .values(
                status=RunStatus.RUNNING,
                interrupt_reason=None,
                finished_at=None,
                chunk_limit=chunk_limit,
            )
        ).rowcount
        session.commit()
        if not won:
            raise api_error(
                CONFLICT, f"A cleaning run is already in progress (run_id={run_id}).", 409
            )
        resumed = True
    else:
        run = Run(user_id=user_id, chunk_limit=chunk_limit, status=RunStatus.RUNNING)
        session.add(run)
        session.commit()
        run_id = run.id

    background_tasks.add_task(_start_run_task, run_id=run_id, user_id=user_id)
    return ok({"run_id": run_id, "resumed": resumed})


@router.get("/api/runs")
def list_runs(
    user_id: str = Depends(require_current_user),
    session: Session = Depends(get_session),
) -> dict:
    runs = (
        session.execute(
            select(Run).where(Run.user_id == user_id).order_by(Run.started_at.desc())
        )
        .scalars()
        .all()
    )
    return ok({"runs": [run_card(session, run) for run in runs]})


@router.get("/api/runs/{run_id}")
def get_run(
    run_id: str,
    user_id: str = Depends(require_current_user),
    session: Session = Depends(get_session),
) -> dict:
    run = load_run(session, run_id, user_id)
    decisions = session.execute(
        select(ThreadDecision, Category.name)
        .join(Category, ThreadDecision.category_id == Category.id)
        .where(ThreadDecision.run_id == run.id, ThreadDecision.user_id == user_id)
        .order_by(ThreadDecision.decided_at.desc())
    ).all()
    card = run_card(session, run)
    card["decisions"] = [
        {
            "gmail_thread_id": decision.gmail_thread_id,
            "sender": decision.sender,
            "subject": decision.subject,
            "snippet": decision.snippet,
            "category": category_name,
            "confidence": decision.confidence,
            "reason": decision.reason,
            "needs_review": decision.needs_review,
            "source": decision.source,
            "undone": decision.undone,
            "decided_at": iso(decision.decided_at),
        }
        for decision, category_name in decisions
    ]
    return ok(card)


def _undo_run_task(*, run_id: str, user_id: str) -> None:
    """Execute the whole-run undo in the background. Never raises into the
    server; ``undo_run`` commits row by row, so any crash leaves a resumable
    state (re-triggering continues from the first non-undone mutation)."""
    from db.session import create_db_session
    from tools.undo import undo_run

    try:
        with create_db_session() as session:
            run = session.get(Run, run_id)
            if run is None:
                return
            mutator = gmail_mutator_for_user(session, user_id)
            lookup = label_lookup_for_user(session, user_id)
            try:
                undo_run(session, run=run, mutator=mutator, label_lookup=lookup)
            except ReauthRequired:
                _mark_needs_reconnect(session, user_id)
                session.commit()
    except Exception:  # noqa: BLE001 — undo is resumable; never crash the server
        _log.warning("runs.undo_background_failed run_id=%s", run_id, exc_info=True)
    finally:
        with _undo_lock:
            _undo_in_flight.discard(run_id)


@router.post("/api/runs/{run_id}/undo")
def undo_run_route(
    run_id: str,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(require_current_user),
    session: Session = Depends(get_session),
) -> dict:
    """Whole-run undo. 409 if the run is active or already undone.

    Starts the undo in the background and returns immediately — replaying a
    50-thread run against Gmail takes tens of seconds, far beyond proxy
    timeouts. Progress streams as ``undo_*`` events on
    ``GET /api/runs/{id}/events``; completion is visible as ``undone_at`` on
    the run card, which the UI polls for.
    """
    run = load_run(session, run_id, user_id)
    if run.status == RunStatus.RUNNING:
        raise api_error(CONFLICT, "This run is still in progress — wait for it to finish.", 409)
    if run.status == RunStatus.UNDONE:
        raise api_error(CONFLICT, "This run has already been undone.", 409)

    _connected_account(session, user_id)  # gmail_reconnect surfaces before we accept

    with _undo_lock:
        if run_id in _undo_in_flight:
            raise api_error(CONFLICT, "An undo for this run is already in progress.", 409)
        _undo_in_flight.add(run_id)

    background_tasks.add_task(_undo_run_task, run_id=run_id, user_id=user_id)
    return ok({"run_id": run_id, "undo_started": True})
