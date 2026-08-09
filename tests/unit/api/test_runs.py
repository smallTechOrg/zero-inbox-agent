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
            "fetch_after": None,
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


def test_only_new_resolves_the_cutoff_to_the_last_completed_runs_start_time(
    client, db, seed, sign_in, captured_task
):
    """only_new must fetch strictly what's new since the user's own last
    completed run — not re-list-and-reclassify the whole inbox again."""
    from db.models import TriageRun

    sign_in("user-alice")
    res = client.post("/api/connections/conn-alice/triage", json={"only_new": True})
    assert res.status_code == 200

    expected_run = db.get(TriageRun, "run-alice")
    assert captured_task[0]["fetch_after"] == expected_run.started_at.isoformat()


def test_only_new_with_no_prior_completed_run_degrades_to_a_full_fetch(
    client, db, sign_in
):
    """A brand-new user has nothing to be "newer than" yet — only_new must not
    error, it must just fetch everything, same as a normal first run."""
    from db.models import ChannelAccount, User, UserSettings

    db.add(User(id="user-new", email="new@example.com", display_name="New"))
    db.add(UserSettings(user_id="user-new"))
    db.add(
        ChannelAccount(
            id="conn-new",
            user_id="user-new",
            channel="gmail",
            account_email="new@gmail.com",
            refresh_token_enc="ENCRYPTED",
            scopes=["gmail.readonly"],
            status="connected",
        )
    )
    db.commit()

    calls: list[dict] = []
    import api.connections as connections_module

    orig = connections_module._run_triage_task
    connections_module._run_triage_task = lambda **kw: calls.append(kw)
    try:
        sign_in("user-new")
        res = client.post("/api/connections/conn-new/triage", json={"only_new": True})
    finally:
        connections_module._run_triage_task = orig

    assert res.status_code == 200
    assert calls[0]["fetch_after"] is None


def test_only_new_ignores_a_cancelled_or_failed_run_as_the_cutoff(
    client, db, seed, sign_in, captured_task
):
    """A cancelled/failed run never got real coverage of the inbox, so it must
    not be trusted as "I already saw everything up to here"."""
    from datetime import datetime, timedelta, timezone

    from db.models import TriageRun

    db.add(
        TriageRun(
            id="run-alice-cancelled",
            user_id="user-alice",
            channel_account_id="conn-alice",
            status="cancelled",
            dry_run=True,
            items_total=10,
            items_decided=0,
            counts={},
            started_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
    )
    db.commit()

    sign_in("user-alice")
    res = client.post("/api/connections/conn-alice/triage", json={"only_new": True})
    assert res.status_code == 200

    completed_run = db.get(TriageRun, "run-alice")
    assert captured_task[0]["fetch_after"] == completed_run.started_at.isoformat()


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


def test_latest_run_resumes_the_last_completed_run_without_retriaging(
    client, seed, sign_in
):
    """Regression: the dashboard only fetched /api/me on load and never asked for
    a prior run, so a completed triage pass a user already paid for vanished on
    refresh until they clicked Start Triage again."""
    sign_in("user-alice")
    res = client.get("/api/runs/latest")
    assert res.status_code == 200
    data = res.json()["data"]

    assert data["id"] == "run-alice"
    assert data["status"] == "completed"


def test_latest_run_is_null_when_the_user_has_never_run_triage(
    client, db, sign_in
):
    from db.models import ChannelAccount, User, UserSettings

    db.add(User(id="user-new", email="new@example.com", display_name="New"))
    db.add(UserSettings(user_id="user-new"))
    db.commit()

    sign_in("user-new")
    res = client.get("/api/runs/latest")
    assert res.status_code == 200
    assert res.json()["data"] is None


def test_latest_run_ignores_a_cancelled_run_behind_a_completed_one(
    client, db, seed, sign_in
):
    """A cancelled/failed run must never bury a prior good completed run —
    same guarantee GET /api/triage/clusters already relies on."""
    from datetime import datetime, timedelta, timezone

    from db.models import TriageRun

    db.add(
        TriageRun(
            id="run-alice-cancelled",
            user_id="user-alice",
            channel_account_id="conn-alice",
            status="cancelled",
            dry_run=True,
            items_total=10,
            items_decided=0,
            counts={},
            started_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
    )
    db.commit()

    sign_in("user-alice")
    res = client.get("/api/runs/latest")
    assert res.json()["data"]["id"] == "run-alice"


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
