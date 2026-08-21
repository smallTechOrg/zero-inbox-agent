"""Unit tests — runs-undo-api slice: the per-run SSE route (replay, live, ownership)."""

from __future__ import annotations

import json
import threading
import time

import pytest

from tests.conftest import seed_user
from tests.unit.test_runs_api import ALICE, BOB, make_client


@pytest.fixture
def make_run(db_session):
    def _make(status: str = "completed", user_id: str = ALICE):
        seed_user(db_session, user_id=user_id, email=f"{user_id}@example.com")
        from db.models import Run

        run = Run(user_id=user_id, status=status)
        db_session.add(run)
        db_session.commit()
        return run

    return _make


def _record(db_session, run, sentences: list[str]):
    from events.store import record_run_event

    for sentence in sentences:
        record_run_event(
            db_session, user_id=run.user_id, run_id=run.id, type="action", sentence=sentence
        )
    db_session.commit()


def _read_stream(client, url: str) -> list[dict]:
    """Read every data frame to stream end (finite on/after a terminal event)."""
    frames: list[dict] = []
    with client.stream("GET", url) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        for line in resp.iter_lines():
            if line.startswith("data: "):
                frames.append(json.loads(line[len("data: "):]))
    return frames


def test_stream_replays_persisted_feed_in_order_then_ends(db_session, make_run):
    run = make_run("completed")
    _record(db_session, run, ["one", "two", "three"])
    with make_client(ALICE) as client:
        frames = _read_stream(client, f"/api/runs/{run.id}/events")
    assert [f["sentence"] for f in frames[:3]] == ["one", "two", "three"]
    assert [f["seq"] for f in frames[:3]] == [1, 2, 3]
    assert all(f["run_id"] == run.id for f in frames)
    assert frames[-1]["type"] == "stream_end"  # terminal run: replay then close


def test_after_seq_resumes_the_feed_where_the_client_left_off(db_session, make_run):
    run = make_run("completed")
    _record(db_session, run, ["one", "two", "three"])
    with make_client(ALICE) as client:
        frames = _read_stream(client, f"/api/runs/{run.id}/events?after_seq=2")
    assert [f["seq"] for f in frames if f["type"] == "action"] == [3]


def test_live_events_stream_after_replay_without_duplicates(db_session, make_run):
    """Reload-mid-run: replay the feed so far, then continue live to the end."""
    run = make_run("running")
    _record(db_session, run, ["already persisted"])

    def _produce_live():
        # record_event persists (own session) THEN emits — the live-tail path.
        from events.store import record_event

        time.sleep(0.3)  # let the stream subscribe first
        record_event(user_id=ALICE, run_id=run.id, type="action", sentence="live one")
        record_event(
            user_id=ALICE, run_id=run.id, type="run_finished", sentence="Run finished."
        )

    producer = threading.Thread(target=_produce_live)
    producer.start()
    try:
        with make_client(ALICE) as client:
            frames = _read_stream(client, f"/api/runs/{run.id}/events")
    finally:
        producer.join()

    data = [f for f in frames if f["type"] not in ("heartbeat", "stream_end")]
    assert [f["sentence"] for f in data] == ["already persisted", "live one", "Run finished."]
    assert [f["seq"] for f in data] == [1, 2, 3]  # no duplicates across the boundary
    assert frames[-1]["type"] == "stream_end"


def test_foreign_or_unknown_run_is_404_not_an_open_stream(db_session, make_run):
    run = make_run("completed")
    seed_user(db_session, user_id=BOB, email="bob@example.com")
    with make_client(BOB) as client:
        assert client.get(f"/api/runs/{run.id}/events").status_code == 404
        assert client.get("/api/runs/does-not-exist/events").status_code == 404
