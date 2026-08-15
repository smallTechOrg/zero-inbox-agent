"""Persist-then-emit for run feed events (spec/capabilities/live-activity-feed.md).

CROSS-SLICE CONTRACT — every feed event a run (or an undo) produces goes
through :func:`record_run_event`:

1. the event is written as a ``run_events`` row (monotonic ``seq`` per run)
   *before* it is emitted, so a page reload mid-run replays the full feed via
   ``GET /api/runs/{id}/events?after_seq=…``;
2. it is then pushed onto the in-process bus (:mod:`events.bus`) for every
   live SSE subscriber.

Event types are the closed set in :class:`domain.enums.RunEventType`.
``sentence`` is one plain-English, present-tense line ("Filed 'ACME invoice'
→ Finance — archived"); machine detail (reasoning, confidence, tokens, cost)
rides in ``detail_json`` and is rendered behind an expander in the UI.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import RunEvent
from events import bus

_log = logging.getLogger("zero_inbox.events.store")


def event_payload(row: RunEvent) -> dict:
    """The wire shape of one feed event — identical for replay and live."""
    return {
        "type": row.type,
        "run_id": row.run_id,
        "seq": row.seq,
        "sentence": row.sentence,
        "detail": row.detail_json or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def next_seq(session: Session, *, run_id: str) -> int:
    current = session.execute(
        select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id)
    ).scalar_one()
    return int(current or 0) + 1


def record_run_event(
    session: Session,
    *,
    user_id: str,
    run_id: str,
    type: str,
    sentence: str,
    detail: dict[str, Any] | None = None,
) -> dict:
    """Persist one feed event, flush it, emit it live. Returns the payload.

    The caller owns the transaction; the row is flushed here so ``seq`` is
    settled and the persisted-before-emitted ordering holds within the caller's
    commit. Emission never raises (bus contract), and a bus problem never
    unwinds the run.
    """
    row = RunEvent(
        user_id=user_id,
        run_id=run_id,
        seq=next_seq(session, run_id=run_id),
        type=str(type),
        sentence=str(sentence),
        detail_json=dict(detail or {}),
    )
    session.add(row)
    session.flush()
    payload = event_payload(row)
    bus.emit(user_id, payload)
    return payload


def record_event(
    *,
    user_id: str,
    run_id: str,
    type: str,
    sentence: str,
    detail: dict[str, Any] | None = None,
) -> dict:
    """Session-owning convenience: persist + commit + emit one event."""
    from db.session import create_db_session

    with create_db_session() as session:
        return record_run_event(
            session,
            user_id=user_id,
            run_id=run_id,
            type=type,
            sentence=sentence,
            detail=detail,
        )


def replay_run_events(
    session: Session, *, user_id: str, run_id: str, after_seq: int = 0
) -> list[dict]:
    """Persisted events for one run after ``after_seq``, oldest first.

    ``user_id`` is part of the WHERE clause — a run id is not a capability.
    """
    rows = (
        session.execute(
            select(RunEvent)
            .where(
                RunEvent.user_id == user_id,
                RunEvent.run_id == run_id,
                RunEvent.seq > int(after_seq),
            )
            .order_by(RunEvent.seq.asc())
        )
        .scalars()
        .all()
    )
    return [event_payload(row) for row in rows]
