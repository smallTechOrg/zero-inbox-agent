"""SSE feed for one run (spec/api.md): ``GET /api/runs/{run_id}/events``.

Replays persisted ``run_events`` rows from ``?after_seq`` (default 0 — the
whole feed), then streams live events from the in-process bus, filtered to
this run. Because every producer persists **before** it emits
(:mod:`events.store`), a page reload mid-run shows the complete feed so far
and continues live; ``seq`` de-duplicates the replay/live boundary.

Frames are ``data: <json>\\n\\n``; a heartbeat frame keeps the connection
alive every 15 s. Ownership is checked before the stream opens: another
user's run id is a plain 404.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from api._common import not_found
from api.runs import require_current_user
from db.models import Run
from events import bus
from events.store import replay_run_events

router = APIRouter()

HEARTBEAT_SECONDS = 15

#: Statuses after which no further live event can arrive on this connection —
#: the stream replays and then ends (a reconnect replays any later undo feed
#: via ``?after_seq``; the undo POST itself returns synchronously).
TERMINAL_RUN_STATUSES = {"completed", "interrupted", "undone"}

#: Feed events that end the live tail: the run (or its undo) is finished.
TERMINAL_EVENT_TYPES = {"run_finished", "run_interrupted", "undo_finished"}


def _frame(payload: dict) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


@router.get("/api/runs/{run_id}/events")
async def run_events_stream(
    run_id: str,
    after_seq: int = Query(default=0, ge=0),
    user_id: str = Depends(require_current_user),
) -> StreamingResponse:
    from db.session import create_db_session

    # Ownership + replay resolved before the stream opens (404 must be a real
    # status code, not a mid-stream error frame).
    with create_db_session() as session:
        run = session.get(Run, run_id)
        if run is None or run.user_id != user_id:
            raise not_found("Run")
        terminal = run.status in TERMINAL_RUN_STATUSES
        replay = replay_run_events(
            session, user_id=user_id, run_id=run_id, after_seq=after_seq
        )

    async def _generate():
        last_seq = after_seq
        for event in replay:
            last_seq = max(last_seq, int(event.get("seq") or 0))
            yield _frame(event)

        if terminal:
            # Nothing more can arrive on this connection; end cleanly rather
            # than holding an EventSource open on a finished run.
            yield _frame({"type": "stream_end", "run_id": run_id, "seq": last_seq})
            return

        queue = bus.subscribe(user_id)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield 'data: {"type":"heartbeat"}\n\n'
                    continue
                if event.get("run_id") != run_id:
                    continue  # this channel carries exactly one run's feed
                seq = int(event.get("seq") or 0)
                if seq and seq <= last_seq:
                    continue  # already replayed from the store
                last_seq = max(last_seq, seq)
                yield _frame(event)
                if event.get("type") in TERMINAL_EVENT_TYPES:
                    yield _frame({"type": "stream_end", "run_id": run_id, "seq": last_seq})
                    return
        except GeneratorExit:
            pass
        finally:
            bus.unsubscribe(user_id, queue)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
