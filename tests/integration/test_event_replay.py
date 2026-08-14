"""Replay on connect (Rule J) — a late joiner sees the run, not an empty feed.

`GET /api/events` flushes `_bus().replay_buffer(user_id)` (1000 events) before it
streams anything live. Phase 7 *verifies* that end to end; it does not rebuild
it — `src/api/events.py` is untouched by this slice.

These run against a REAL uvicorn server on an ephemeral port, because an SSE
stream is exactly what the in-process test transports cannot express: both
`TestClient` and `httpx.ASGITransport` wait for the response body to finish, and
this body never finishes. Every read below is bounded by `asyncio.wait_for`, so
a broken stream fails fast instead of hanging the suite.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from api import app
from api.session import COOKIE_NAME, issue_session_token
from events import heartbeat

USER_ID = "replay-user"
READ_TIMEOUT_S = 10.0



def _bus():
    """Resolve the bus THE SAME WAY THE SERVER DOES.

    ``src/api/events.py`` does ``from events import bus`` at import time and
    holds that module object for the life of the process. ``tests/unit/events/
    test_bus.py`` deletes both ``events.bus`` and ``events`` from
    ``sys.modules``, so ``importlib.import_module("events.bus")`` would build a
    BRAND-NEW module with an empty ring: the test would seed one ring while the
    live server replayed from another. Reading the attribute off ``api.events``
    gives us the exact object the streaming endpoint reads from, whatever
    ``sys.modules`` currently holds.
    """
    import api.events

    return api.events.bus


def _repair_bus_module_identity() -> None:
    """Make ``from events import bus`` resolve to the object the server uses.

    ``tests/unit/events/test_bus.py`` deletes ``events`` and ``events.bus`` from
    ``sys.modules``. ``src/api/events.py`` bound its ``bus`` at import time and is
    unaffected — but ``events.heartbeat`` imports the bus *lazily*, inside
    ``_publish``, so after that teardown the watchdog would emit into a brand-new
    empty ring while the live endpoint replayed from the old one. Rebinding the
    package attribute (rather than reaching for the fresh module) keeps every
    producer and the consumer on one ring, which is the invariant that holds in
    production and the one these tests exist to verify.
    """
    import importlib
    import sys

    import api.events

    package = sys.modules.get("events") or importlib.import_module("events")
    sys.modules["events"] = package
    sys.modules["events.bus"] = api.events.bus
    package.bus = api.events.bus


@pytest.fixture(autouse=True)
def _clean_bus(_isolated_db):
    _repair_bus_module_identity()
    heartbeat.stop_watchdog()
    _bus()._rings.clear()
    yield
    _bus()._rings.clear()
    heartbeat.stop_watchdog()


@pytest.fixture
def server_url(_clean_bus):
    """A real HTTP server for the real app, on an ephemeral port.

    Function-scoped and explicitly downstream of ``_clean_bus`` (and therefore of
    ``_isolated_db``) so the server can never come up bound to the real
    production database.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="sse-test-server", daemon=True)
    thread.start()

    deadline = time.monotonic() + 15.0
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "the test server did not start within 15s"

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=10.0)


def _client(server_url: str, *, authenticated: bool = True) -> httpx.AsyncClient:
    cookies = {COOKIE_NAME: issue_session_token(USER_ID)} if authenticated else {}
    return httpx.AsyncClient(
        base_url=server_url,
        cookies=cookies,
        timeout=READ_TIMEOUT_S,
    )


def _seed(count: int, *, start: int = 0) -> list[dict]:
    events = [
        {
            "type": "thread_classified",
            "run_id": "run-late-join",
            "item_id": f"t{i}",
            "category": "newsletters",
            "action": "archive",
        }
        for i in range(start, start + count)
    ]
    for event in events:
        _bus().emit(USER_ID, event)
    return events


async def _read_frames(lines, count: int) -> list[dict]:
    """Read exactly *count* SSE data frames. Every await is bounded."""
    frames: list[dict] = []
    while len(frames) < count:
        line = await asyncio.wait_for(anext(lines), timeout=READ_TIMEOUT_S)
        if not line or not line.startswith("data: "):
            continue
        frames.append(json.loads(line[len("data: ") :]))
    return frames


async def test_late_joiner_receives_the_run_history_before_any_live_event(server_url):
    """A client connecting MID-RUN paints a populated feed immediately."""
    seeded = _seed(220)
    assert len(_bus().replay_buffer(USER_ID)) == 220

    async with _client(server_url) as client:
        async with client.stream("GET", "/api/events") as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")

            lines = response.aiter_lines()
            replayed = await _read_frames(lines, 220)

            assert len(replayed) == 220, "the late joiner got a partial (or empty) feed"
            assert [f["item_id"] for f in replayed] == [e["item_id"] for e in seeded]

            # Only now does a live event arrive — history first, live second.
            _bus().emit(USER_ID, {"type": "run_completed", "run_id": "run-late-join"})
            live = await _read_frames(lines, 1)
            assert live[0]["type"] == "run_completed"


async def test_ring_buffer_holds_a_thousand_events_for_a_long_run(server_url):
    _seed(1200)

    buffered = _bus().replay_buffer(USER_ID)
    assert len(buffered) == 1000, "the ring must carry a multi-minute run, not seconds of it"
    # The ring drops the OLDEST, so a joiner sees the most recent history.
    assert buffered[0]["item_id"] == "t200"
    assert buffered[-1]["item_id"] == "t1199"

    async with _client(server_url) as client:
        async with client.stream("GET", "/api/events") as response:
            replayed = await _read_frames(response.aiter_lines(), 1000)

    assert len(replayed) == 1000
    assert replayed[0]["item_id"] == "t200"
    assert replayed[-1]["item_id"] == "t1199"


async def test_replay_is_scoped_to_the_authenticated_user(server_url):
    _seed(5)
    _bus().emit("someone-else", {"type": "thread_classified", "item_id": "leak"})

    async with _client(server_url) as client:
        async with client.stream("GET", "/api/events") as response:
            replayed = await _read_frames(response.aiter_lines(), 5)

    assert [f["item_id"] for f in replayed] == ["t0", "t1", "t2", "t3", "t4"]
    assert all(f.get("item_id") != "leak" for f in replayed)


async def test_events_stream_requires_a_session(server_url):
    async with _client(server_url, authenticated=False) as client:
        response = await client.get("/api/events")
    assert response.status_code == 401


async def test_heartbeats_reach_a_late_joiner_through_the_same_replay(server_url):
    """A joiner arriving during a silent LLM call sees the watchdog's beats."""
    heartbeat.note_activity(
        USER_ID,
        run_id="run-late-join",
        phase="triage.batch_dispatched",
        fields={"tier": 3, "batch_n": 3, "batch_total": 9, "batch_size": 29, "model": "nemotron"},
    )
    assert heartbeat.sweep_once(now=_future()) == 1

    async with _client(server_url) as client:
        async with client.stream("GET", "/api/events") as response:
            frames = await _read_frames(response.aiter_lines(), 1)

    assert frames[0]["type"] == "activity_heartbeat"
    assert frames[0]["phase"] == "tier3_classify"
    assert frames[0]["batch_n"] == 3


def _future() -> float:
    import time

    return time.monotonic() + heartbeat.HEARTBEAT_INTERVAL_SECONDS + 0.5
