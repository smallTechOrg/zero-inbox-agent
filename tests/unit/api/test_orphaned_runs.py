"""Orphaned runs must never keep claiming to be live.

Regression: triage runs execute as in-process background tasks, so a server
restart (or crash) kills them while their row still says ``running``. The UI
polls that row and renders a live progress bar for work that stopped long ago —
showing activity that is not happening. Observed for real: a run sat at
``running`` 0/0 for hours across restarts, driving a phantom progress bar.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _make_run(session, run_id: str, status: str, **overrides):
    from db.models import TriageRun

    kwargs = {
        "id": run_id,
        "user_id": "user-alice",
        "channel_account_id": "conn-alice",
        "status": status,
        "dry_run": True,
        "items_total": 0,
        "items_decided": 0,
        "counts": {},
        "started_at": datetime.now(timezone.utc) - timedelta(hours=2),
    }
    kwargs.update(overrides)
    session.add(TriageRun(**kwargs))


def test_a_running_run_is_closed_out_as_failed_on_startup(db, seed):
    from api import _reconcile_orphaned_runs
    from db.models import TriageRun

    _make_run(db, "run-orphan", "running")
    db.commit()

    closed = _reconcile_orphaned_runs()

    db.expire_all()
    run = db.get(TriageRun, "run-orphan")
    assert closed == 1
    assert run.status == "failed"
    assert run.finished_at is not None
    assert "Interrupted" in run.error_message


def test_terminal_runs_are_left_alone(db, seed):
    from api import _reconcile_orphaned_runs
    from db.models import TriageRun

    _make_run(db, "run-done", "completed", finished_at=datetime.now(timezone.utc))
    _make_run(db, "run-cancelled", "cancelled", finished_at=datetime.now(timezone.utc))
    db.commit()

    _reconcile_orphaned_runs()

    db.expire_all()
    assert db.get(TriageRun, "run-done").status == "completed"
    assert db.get(TriageRun, "run-cancelled").status == "cancelled"


def test_reconciliation_closes_every_orphan_not_just_the_first(db, seed):
    from api import _reconcile_orphaned_runs
    from db.models import TriageRun

    for n in range(3):
        _make_run(db, f"run-orphan-{n}", "running")
    db.commit()

    closed = _reconcile_orphaned_runs()

    db.expire_all()
    assert closed == 3
    for n in range(3):
        assert db.get(TriageRun, f"run-orphan-{n}").status == "failed"


def test_a_closed_out_run_is_not_the_default_dashboard_view(client, db, seed, sign_in):
    """The orphan must not become 'latest run' either — a failed run is already
    excluded from the default view, so closing it out actively un-sticks the UI."""
    from api import _reconcile_orphaned_runs

    _make_run(
        db,
        "run-orphan",
        "running",
        started_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db.commit()
    _reconcile_orphaned_runs()

    sign_in("user-alice")
    res = client.get("/api/runs/latest")
    assert res.status_code == 200
    data = res.json()["data"]
    # Falls back to the seeded completed run, never the closed-out orphan.
    assert data is None or data["id"] != "run-orphan"
