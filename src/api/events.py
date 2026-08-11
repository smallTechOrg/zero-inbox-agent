"""GET /api/events — Server-Sent Events stream for the authenticated user.

On connect:
  1. Replays the last 50 buffered events (ring buffer) for the user.
  2. Streams new events as they arrive via the in-memory bus.
  3. Sends a heartbeat every 15 s to keep the connection alive.

Each frame is:  ``data: <json>\\n\\n``
"""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from api.session import require_user_id
from events import bus

router = APIRouter()

_HEARTBEAT_INTERVAL = 15  # seconds


@router.get("/api/events")
async def sse_events(user_id: str = Depends(require_user_id)) -> StreamingResponse:
    """SSE stream — scoped to the authenticated user."""

    async def _generate():
        # 1. Replay buffered events so the client catches up immediately.
        for event in bus.replay_buffer(user_id):
            yield f"data: {json.dumps(event)}\n\n"

        # 2. Subscribe to future events.
        queue = bus.subscribe(user_id)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_INTERVAL)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    # Keep-alive heartbeat — no data, just re-arm the HTTP connection.
                    yield 'data: {"type":"heartbeat"}\n\n'
        except GeneratorExit:
            pass
        finally:
            bus.unsubscribe(user_id, queue)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
