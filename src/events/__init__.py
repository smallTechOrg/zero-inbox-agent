"""Per-run event machinery: in-memory bus (live SSE) + persisted run_events.

Contract for producers (graph runner, undo): call
``events.store.record_run_event(session, …)`` — persist first, emit second.
Consumers (the SSE route) replay from the store and subscribe to the bus.
"""

from typing import Any

from sqlalchemy.orm import Session

from events.bus import emit, replay_buffer, subscribe, unsubscribe
from events.store import (
    event_payload,
    record_event,
    record_run_event,
    replay_run_events,
)


def publish_run_event(
    session: Session,
    *,
    user_id: str,
    run_id: str,
    type: str,
    sentence: str,
    detail: dict[str, Any] | None = None,
) -> dict:
    """Persist-first feed event (graph runner ↔ events contract).

    Writes the ``run_events`` row (monotonic per-run ``seq``) and *then*
    emits it on the in-process bus — see :func:`events.store.record_run_event`.
    """
    return record_run_event(
        session,
        user_id=user_id,
        run_id=run_id,
        type=type,
        sentence=sentence,
        detail=detail,
    )


__all__ = [
    "publish_run_event",
    "emit",
    "replay_buffer",
    "subscribe",
    "unsubscribe",
    "event_payload",
    "record_event",
    "record_run_event",
    "replay_run_events",
]
