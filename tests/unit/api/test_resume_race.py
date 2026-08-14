"""The resume race: concurrent POSTs must spawn exactly ONE worker.

Regression for a defect qa-auditor reproduced twice — 5 concurrent requests
spawned 2 background workers. The pre-existing test only issued two SEQUENTIAL
calls, which never opens the check-then-commit window, so it passed while the
bug was live. A double-click on Resume, or a client retry on a flaky network,
would have had two workers deciding one run: double-counted items_decided and
double-counted cost.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest


@pytest.fixture
def resumable_run(db, seed):
    from db.models import TriageRun

    db.add(
        TriageRun(
            id="run-race", user_id="user-alice", channel_account_id="conn-alice",
            status="resumable", dry_run=True, items_total=100, items_decided=40,
            counts={}, started_at=datetime.now(timezone.utc),
        )
    )
    db.commit()
    return "run-race"


def _spawn_count(client, run_id, n, monkeypatch):
    """Fire n concurrent resumes; return how many background workers were scheduled."""
    import api.connections as connections

    scheduled: list[dict] = []
    lock = threading.Lock()

    def _record(**kw):
        with lock:
            scheduled.append(kw)

    monkeypatch.setattr(connections, "_run_triage_task", _record)

    barrier = threading.Barrier(n)
    results: list[int] = []
    rlock = threading.Lock()

    def _go():
        barrier.wait()  # release all threads at the same instant
        r = client.post(f"/api/runs/{run_id}/resume")
        with rlock:
            results.append(r.status_code)

    threads = [threading.Thread(target=_go) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return scheduled, results


#: One race is a coin flip: against the buggy check-then-commit this test caught
#: the double-worker in only 3 of 6 runs. Repeating the race inside the test
#: turns a ~50% detector into a near-certain one (1 - 0.5**ROUNDS), so a future
#: regression cannot slip through on a lucky run.
ROUNDS = 8


def test_concurrent_resume_starts_exactly_one_worker(
    client, db, seed, sign_in, monkeypatch
):
    from db.models import TriageRun

    sign_in("user-alice")

    for attempt in range(ROUNDS):
        run_id = f"run-race-{attempt}"
        db.add(
            TriageRun(
                id=run_id, user_id="user-alice", channel_account_id="conn-alice",
                status="resumable", dry_run=True, items_total=100, items_decided=40,
                counts={}, started_at=datetime.now(timezone.utc),
            )
        )
        db.commit()

        scheduled, results = _spawn_count(client, run_id, 5, monkeypatch)

        assert all(code == 200 for code in results), (attempt, results)
        assert len(scheduled) == 1, (
            f"round {attempt}: expected exactly 1 background worker, got "
            f"{len(scheduled)} — the resumable->running transition is not atomic"
        )
        db.expire_all()
        assert db.get(TriageRun, run_id).status == "running"
