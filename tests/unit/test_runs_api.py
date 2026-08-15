"""Unit tests — runs-undo-api slice: POST/GET /api/runs (trigger, cards, isolation).

The auth dependency is overridden at the FastAPI layer (the auth slice owns
sessions); the DB is the suite's throwaway SQLite file. The background runner
is faked — the graph slice owns the real ``run_triage`` (spec/agent.md).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import seed_user

ALICE = "test-user-alice"
BOB = "test-user-bob"


def make_client(user_id: str = ALICE):
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    from api import audit, events, runs

    app = FastAPI()

    @app.exception_handler(HTTPException)
    async def _handler(request, exc):
        detail = exc.detail if isinstance(exc.detail, dict) else {
            "code": "http_error",
            "message": str(exc.detail),
        }
        return JSONResponse(status_code=exc.status_code, content={"ok": False, "error": detail})

    for router in (runs.router, events.router, audit.router):
        app.include_router(router)
    app.dependency_overrides[runs.require_current_user] = lambda: user_id
    return TestClient(app)


@pytest.fixture
def alice(db_session):
    return seed_user(db_session, user_id=ALICE, email="alice@example.com")


@pytest.fixture
def client(alice):
    with make_client(ALICE) as c:
        yield c


def _no_op_runner(monkeypatch):
    import api.runs as runs_module

    calls: list[dict] = []
    monkeypatch.setattr(
        runs_module, "_start_run_task", lambda **kw: calls.append(kw)
    )
    return calls


# --- POST /api/runs -------------------------------------------------------


def test_trigger_run_creates_running_run_and_starts_worker(client, db_session, monkeypatch):
    calls = _no_op_runner(monkeypatch)
    resp = client.post("/api/runs", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    run_id = body["data"]["run_id"]
    assert body["data"]["resumed"] is False

    from db.models import Run

    run = db_session.get(Run, run_id)
    assert run is not None
    assert run.user_id == ALICE
    assert run.status == "running"
    assert run.chunk_limit == 50  # spec default
    assert run.trigger == "clean_chunk"
    assert calls == [{"run_id": run_id, "user_id": ALICE}]


def test_trigger_run_honours_chunk_limit_and_validates_it(client, db_session, monkeypatch):
    _no_op_runner(monkeypatch)
    resp = client.post("/api/runs", json={"chunk_limit": 75})
    run_id = resp.json()["data"]["run_id"]

    from db.models import Run

    assert db_session.get(Run, run_id).chunk_limit == 75

    assert client.post("/api/runs", json={"chunk_limit": 0}).status_code == 422


def test_second_trigger_409s_with_active_run_id(client, db_session, monkeypatch):
    _no_op_runner(monkeypatch)
    first = client.post("/api/runs", json={}).json()["data"]["run_id"]
    resp = client.post("/api/runs", json={})
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "conflict"
    assert first in err["message"]  # 409-with-active-run_id (spec/api.md)


def test_trigger_resumes_the_interrupted_run(client, db_session, monkeypatch):
    calls = _no_op_runner(monkeypatch)
    from db.models import Run

    run = Run(
        user_id=ALICE,
        status="interrupted",
        interrupt_reason="rate limited",
        finished_at=datetime.now(timezone.utc),
    )
    db_session.add(run)
    db_session.commit()

    resp = client.post("/api/runs", json={})
    data = resp.json()["data"]
    assert data == {"run_id": run.id, "resumed": True}
    db_session.expire_all()
    resumed = db_session.get(Run, run.id)
    assert resumed.status == "running"
    assert resumed.interrupt_reason is None
    assert resumed.finished_at is None
    assert calls[-1]["run_id"] == run.id


def test_trigger_without_connected_gmail_is_reconnect_not_traceback(db_session, monkeypatch):
    from db import models

    db_session.add(models.User(id=BOB, email="bob@example.com", name="Bob"))
    db_session.commit()  # no gmail_account row at all
    with make_client(BOB) as client:
        resp = client.post("/api/runs", json={})
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "gmail_reconnect"
    assert err["message"] == "Reconnect Gmail to continue."


def test_trigger_with_needs_reconnect_account_is_reconnect(client, db_session, alice, monkeypatch):
    _no_op_runner(monkeypatch)
    from db.models import GmailAccount

    account = db_session.query(GmailAccount).filter_by(user_id=ALICE).one()
    account.status = "needs_reconnect"
    db_session.commit()
    resp = client.post("/api/runs", json={})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "gmail_reconnect"


# --- GET /api/runs, GET /api/runs/{id} ------------------------------------


def _seed_completed_run(db_session, categories, *, user_id=ALICE, decided=2):
    from db.models import Mutation, Run, ThreadDecision

    now = datetime.now(timezone.utc)
    run = Run(
        user_id=user_id,
        status="completed",
        started_at=now - timedelta(minutes=3),
        finished_at=now,
        threads_decided=decided,
        llm_calls=1,
        tokens_in=800,
        tokens_out=120,
        est_cost_usd=0.0012,
        fallback_events=1,
    )
    db_session.add(run)
    db_session.flush()
    names = ["Finance", "Newsletters"]
    for i in range(decided):
        cat = categories[names[i % len(names)]]
        db_session.add(
            ThreadDecision(
                user_id=user_id,
                run_id=run.id,
                gmail_thread_id=f"test-thread-{run.id}-{i}",
                sender=f"sender{i}@example.com",
                subject=f"subject {i}",
                snippet="snippet",
                category_id=cat.id,
                confidence=0.9,
                reason="clearly that category",
                decided_at=now - timedelta(seconds=decided - i),
            )
        )
        db_session.add(
            Mutation(
                user_id=user_id,
                run_id=run.id,
                gmail_thread_id=f"test-thread-{run.id}-{i}",
                action="add_label",
                label_name=cat.name,
                reason="filed",
                applied_at=now - timedelta(seconds=decided - i),
            )
        )
    db_session.commit()
    return run


def test_run_cards_have_counts_cost_and_undo_state(client, db_session, alice):
    _, categories = alice
    run = _seed_completed_run(db_session, categories)
    resp = client.get("/api/runs")
    assert resp.status_code == 200
    cards = resp.json()["data"]["runs"]
    assert [c["id"] for c in cards] == [run.id]
    card = cards[0]
    assert card["status"] == "completed"
    assert card["counts"] == {"Finance": 1, "Newsletters": 1}
    assert card["cost"] == {
        "llm_calls": 1,
        "tokens_in": 800,
        "tokens_out": 120,
        "est_cost_usd": 0.0012,
        "fallback_events": 1,
    }
    assert card["undo"] == {"undone": False, "undone_at": None, "undoable": True}


def test_run_detail_includes_decisions(client, db_session, alice):
    _, categories = alice
    run = _seed_completed_run(db_session, categories)
    detail = client.get(f"/api/runs/{run.id}").json()["data"]
    assert len(detail["decisions"]) == 2
    first = detail["decisions"][0]
    assert set(first) == {
        "gmail_thread_id", "sender", "subject", "snippet", "category",
        "confidence", "reason", "needs_review", "source", "undone", "decided_at",
    }
    assert first["category"] in {"Finance", "Newsletters"}


def test_runs_are_per_user_isolated(client, db_session, alice):
    _, alice_cats = alice
    _seed_completed_run(db_session, alice_cats)
    _, bob_cats = seed_user(db_session, user_id=BOB, email="bob@example.com")
    bob_run = _seed_completed_run(db_session, bob_cats, user_id=BOB)

    cards = client.get("/api/runs").json()["data"]["runs"]
    assert all(bob_run.id != c["id"] for c in cards)
    assert client.get(f"/api/runs/{bob_run.id}").status_code == 404
