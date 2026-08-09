"""Launching a triage run, polling its persisted progress, and cancelling it."""

import pytest


@pytest.fixture
def captured_task(monkeypatch):
    """Replaces the background worker with a recorder (the graph is a sibling slice)."""
    calls: list[dict] = []
    import api.connections as connections

    monkeypatch.setattr(connections, "_run_triage_task", lambda **kw: calls.append(kw))
    return calls


def test_start_triage_creates_a_running_dry_run_and_returns_immediately(
    client, db, seed, sign_in, captured_task
):
    from db.models import TriageRun

    sign_in("user-alice")
    res = client.post("/api/connections/conn-alice/triage", json={"limit": 200})
    assert res.status_code == 200

    run_id = res.json()["data"]["run_id"]
    run = db.get(TriageRun, run_id)
    assert run is not None
    assert run.user_id == "user-alice"
    assert run.channel_account_id == "conn-alice"
    assert run.status == "running"
    assert run.dry_run is True

    assert captured_task == [
        {
            "run_id": run_id,
            "user_id": "user-alice",
            "channel_account_id": "conn-alice",
            "limit": 200,
        }
    ]


def test_start_triage_defaults_to_200_threads(client, seed, sign_in, captured_task):
    sign_in("user-alice")
    res = client.post("/api/connections/conn-alice/triage")
    assert res.status_code == 200
    assert captured_task[0]["limit"] == 200


def test_start_triage_rejects_an_out_of_range_limit(client, seed, sign_in, captured_task):
    sign_in("user-alice")
    res = client.post("/api/connections/conn-alice/triage", json={"limit": 0})
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "validation_error"
    assert captured_task == []


def test_start_triage_on_another_users_connection_is_not_found(
    client, seed, sign_in, captured_task
):
    sign_in("user-alice")
    res = client.post("/api/connections/conn-bob/triage", json={"limit": 10})
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "not_found"
    assert captured_task == []


def test_start_triage_on_a_stale_connection_demands_reauth(
    client, db, seed, sign_in, captured_task
):
    from db.models import ChannelAccount

    account = db.get(ChannelAccount, "conn-alice")
    account.status = "reauth_required"
    db.commit()

    sign_in("user-alice")
    res = client.post("/api/connections/conn-alice/triage", json={"limit": 10})
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "reauth_required"
    assert captured_task == []


def test_start_triage_requires_a_session(client, seed, captured_task):
    res = client.post("/api/connections/conn-alice/triage", json={"limit": 10})
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "unauthenticated"


def test_background_task_records_a_crash_on_the_run_row(db, seed, monkeypatch):
    import sys
    import types

    from api.connections import _run_triage_task
    from db.models import TriageRun

    fake = types.ModuleType("graph.runner")

    def boom(**kwargs):
        raise RuntimeError("gmail exploded")

    fake.run_triage = boom
    monkeypatch.setitem(sys.modules, "graph.runner", fake)

    run = db.get(TriageRun, "run-alice")
    run.status = "running"
    run.finished_at = None
    db.commit()

    _run_triage_task(
        run_id="run-alice", user_id="user-alice", channel_account_id="conn-alice", limit=5
    )

    db.expire_all()
    run = db.get(TriageRun, "run-alice")
    assert run.status == "failed"
    assert "gmail exploded" in run.error_message
    assert run.finished_at is not None


def test_get_run_returns_the_persisted_progress_payload(client, seed, sign_in):
    sign_in("user-alice")
    res = client.get("/api/runs/run-alice")
    assert res.status_code == 200
    data = res.json()["data"]

    assert data["id"] == "run-alice"
    assert data["status"] == "completed"
    assert data["dry_run"] is True
    assert data["items_total"] == 3
    assert data["items_decided"] == 3
    assert data["counts"]["needs_your_call"] == 1
    assert data["cost"] == {"tokens_in": 120, "tokens_out": 40, "usd": 0.0021}
    assert data["error_message"] is None
    assert data["started_at"] and data["finished_at"]


def test_get_another_users_run_is_not_found(client, seed, sign_in):
    sign_in("user-alice")
    res = client.get("/api/runs/run-bob")
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "not_found"


def test_get_unknown_run_is_not_found(client, seed, sign_in):
    sign_in("user-alice")
    assert client.get("/api/runs/nope").status_code == 404


def test_cancel_marks_a_running_run_cancelled(client, db, seed, sign_in):
    from db.models import TriageRun

    run = db.get(TriageRun, "run-alice")
    run.status = "running"
    run.finished_at = None
    db.commit()

    sign_in("user-alice")
    res = client.post("/api/runs/run-alice/cancel")
    assert res.status_code == 200
    assert res.json()["data"]["status"] == "cancelled"

    db.expire_all()
    run = db.get(TriageRun, "run-alice")
    assert run.status == "cancelled"
    assert run.finished_at is not None


def test_cancel_of_a_finished_run_reports_its_real_status(client, db, seed, sign_in):
    from db.models import TriageRun

    sign_in("user-alice")
    res = client.post("/api/runs/run-alice/cancel")
    assert res.status_code == 200
    assert res.json()["data"]["status"] == "completed"

    db.expire_all()
    assert db.get(TriageRun, "run-alice").status == "completed"


def test_cancel_of_another_users_run_is_not_found(client, seed, sign_in):
    sign_in("user-alice")
    assert client.post("/api/runs/run-bob/cancel").status_code == 404
