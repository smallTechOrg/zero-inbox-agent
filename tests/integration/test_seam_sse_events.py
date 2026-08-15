"""Seam: run → run_events (persisted) → SSE replay.

spec/capabilities/live-activity-feed.md: events are persisted BEFORE emission;
a reload replays the full feed via ``?after_seq``; every decision and mutation
has exactly one feed event.
"""

from __future__ import annotations

import json

import pytest

from tests.fixtures.fake_gmail import install_inbox
from tests.fixtures.threads import sample_threads
from tests.integration._helpers import envelope_ok, run_to_completion

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture
def completed_run(auth_client, seeded_user, monkeypatch, _require_llm_key):
    install_inbox(monkeypatch, sample_threads(3))
    run_id, run = run_to_completion(auth_client, chunk_limit=3)
    assert run["status"] == "completed"
    return run_id


def _persisted_events(db_session, run_id):
    from db import models

    return (
        db_session.query(models.RunEvent)
        .filter(models.RunEvent.run_id == run_id)
        .order_by(models.RunEvent.seq)
        .all()
    )


def _stream_events(client, run_id, after_seq=0, max_events=200):
    """Read SSE frames until run_finished/undo terminal event or stream end."""
    events = []
    with client.stream("GET", f"/api/runs/{run_id}/events?after_seq={after_seq}") as response:
        assert response.status_code == 200
        assert "text/event-stream" in response.headers.get("content-type", "")
        data_lines: list[str] = []
        for raw in response.iter_lines():
            line = raw.decode() if isinstance(raw, bytes) else raw
            if line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
            elif line == "" and data_lines:
                try:
                    events.append(json.loads("\n".join(data_lines)))
                except json.JSONDecodeError:
                    events.append({"raw": "\n".join(data_lines)})
                data_lines = []
                last = events[-1]
                if last.get("type") in ("run_finished", "undo_finished") or len(events) >= max_events:
                    break
    return events


class TestPersistence:
    def test_the_full_feed_is_persisted_with_ordered_seq(self, completed_run, db_session):
        rows = _persisted_events(db_session, completed_run)
        assert rows, "no run_events persisted — the run → events seam is cut"
        seqs = [r.seq for r in rows]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "seq must be strictly ordered"
        types = {r.type for r in rows}
        assert "chunk_loaded" in types
        assert "decision" in types
        assert "run_finished" in types
        for r in rows:
            assert r.sentence and r.sentence.strip(), "every event is a plain-English sentence"
            assert "{" not in r.sentence, f"sentence looks like raw JSON: {r.sentence!r}"

    def test_exactly_one_event_per_decision_and_per_mutation(self, completed_run, db_session):
        from db import models

        rows = _persisted_events(db_session, completed_run)
        n_decision_events = sum(1 for r in rows if r.type == "decision")
        n_action_events = sum(1 for r in rows if r.type == "action")
        n_decisions = (
            db_session.query(models.ThreadDecision)
            .filter(models.ThreadDecision.run_id == completed_run)
            .count()
        )
        n_mutations = (
            db_session.query(models.Mutation)
            .filter(models.Mutation.run_id == completed_run)
            .count()
        )
        assert n_decision_events == n_decisions, (
            f"{n_decisions} decisions but {n_decision_events} decision events"
        )
        assert n_action_events == n_mutations, (
            f"{n_mutations} mutations but {n_action_events} action events"
        )

    def test_decision_events_carry_reasoning_in_detail_json(self, completed_run, db_session):
        rows = [r for r in _persisted_events(db_session, completed_run) if r.type == "decision"]
        for r in rows:
            detail = r.detail_json if isinstance(r.detail_json, dict) else json.loads(r.detail_json or "{}")
            blob = str(detail).lower()
            assert "confidence" in blob or "reason" in blob, (
                "decision events must carry reasoning + confidence for the expander"
            )


class TestReplay:
    def test_reconnect_replays_the_persisted_feed(self, auth_client, completed_run, db_session):
        streamed = _stream_events(auth_client, completed_run, after_seq=0)
        assert streamed, "SSE replay returned nothing for a completed run"
        persisted = _persisted_events(db_session, completed_run)
        assert len(streamed) == len(persisted), (
            f"replay lost events: streamed {len(streamed)} vs persisted {len(persisted)}"
        )
        assert streamed[-1].get("type") == "run_finished"

    def test_after_seq_skips_already_seen_events(self, auth_client, completed_run, db_session):
        persisted = _persisted_events(db_session, completed_run)
        midpoint = persisted[len(persisted) // 2].seq
        tail = _stream_events(auth_client, completed_run, after_seq=midpoint)
        assert len(tail) == len([r for r in persisted if r.seq > midpoint]), (
            "?after_seq must replay only events newer than the cursor"
        )

    def test_another_users_run_events_are_not_readable(self, api_client, completed_run, db_session):
        from tests.conftest import seed_user, session_cookie_for

        seed_user(db_session, "test-user-mallory", "mallory@example.com")
        name, value = session_cookie_for("test-user-mallory")
        api_client.cookies.set(name, value)
        response = api_client.get(f"/api/runs/{completed_run}/events?after_seq=0")
        assert response.status_code in (403, 404), (
            "cross-user leak: Mallory can open Alice's event stream"
        )


class TestUndoEvents:
    def test_undo_streams_and_persists_undo_events_on_the_same_channel(
        self, auth_client, completed_run, db_session
    ):
        envelope_ok(auth_client.post(f"/api/runs/{completed_run}/undo"))
        db_session.expire_all()
        rows = _persisted_events(db_session, completed_run)
        undo_types = [r.type for r in rows if r.type.startswith("undo")]
        assert undo_types, "undo produced no undo_* feed events"
