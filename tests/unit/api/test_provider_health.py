"""GET /api/provider-health — envelope, payload shape and auth scoping."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm import health, throttle

PRIMARY = "nvidia/nemotron-3-nano-30b-a3b"
SECOND = "nvidia/nemotron-3-super-120b-a12b"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("AGENT_LLM_MAX_RPM", "350")
    throttle.reset()
    health.reset_all()
    yield
    throttle.reset()
    health.reset_all()


@pytest.fixture
def health_client(_isolated_db):
    """A minimal app carrying only this slice's router.

    The router is mounted on the real app by slice 1; testing it standalone keeps
    this slice's gate independent of that slice's landing order.
    """
    from api import _install_handlers
    from api.provider_health import router

    app = FastAPI()
    _install_handlers(app)
    app.include_router(router)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def signed_in(health_client):
    def _sign_in(user_id: str) -> None:
        from api.session import COOKIE_NAME, issue_session_token

        health_client.cookies.set(COOKIE_NAME, issue_session_token(user_id))

    return _sign_in


def test_requires_authentication(health_client):
    response = health_client.get("/api/provider-health")
    assert response.status_code == 401
    body = response.json()
    assert body["data"] is None
    assert body["error"]["code"] == "unauthenticated"


def test_reports_chain_position_throttle_and_counters(health_client, signed_in, seed):
    signed_in("user-alice")
    health.reset("run-alice", user_id="user-alice")
    health.record_call("run-alice")
    health.record_retry("run-alice")

    response = health_client.get("/api/provider-health?run_id=run-alice")
    assert response.status_code == 200
    body = response.json()
    assert body["error"] is None

    data = body["data"]
    assert data["provider"] == "nvidia"
    assert data["model"] == PRIMARY
    assert data["model_chain"][0] == PRIMARY
    assert data["chain_position"] == 0
    assert data["chain_exhausted"] is False
    assert data["circuit_open"] is False
    assert data["calls"] == 1
    assert data["retries"] == 1
    assert data["consecutive_failures"] == 0
    assert data["degraded"] is True  # retries/calls == 1.0
    assert data["throttle"] == {
        "max_rpm": 350,
        "available": data["throttle"]["available"],
        "waiting": 0,
    }
    assert set(data) == {
        "provider",
        "model",
        "model_chain",
        "chain_position",
        "chain_exhausted",
        "circuit_open",
        "calls",
        "retries",
        "consecutive_failures",
        "degraded",
        "run_id",
        "throttle",
    }


def test_reports_the_run_current_model_after_a_fallback(health_client, signed_in, seed):
    signed_in("user-alice")
    health.reset("run-alice", user_id="user-alice")
    health.record_failure("run-alice", model=PRIMARY, error="404")
    health.advance_model("run-alice", reason="model unavailable: 404", from_model=PRIMARY)

    data = health_client.get("/api/provider-health?run_id=run-alice").json()["data"]
    assert data["model"] == SECOND  # not the configured default any more
    assert data["chain_position"] == 1


def test_falls_back_to_the_users_latest_run_when_no_run_id_is_given(
    health_client, signed_in, seed
):
    signed_in("user-alice")
    health.reset("run-alice", user_id="user-alice")

    data = health_client.get("/api/provider-health").json()["data"]
    assert data["run_id"] == "run-alice"


def test_another_users_run_id_is_never_reported_on(health_client, signed_in, seed):
    """Auth scoping: bob may not read alice's run health."""
    signed_in("user-bob")
    health.reset("run-alice", user_id="user-alice")
    health.record_failure("run-alice", model=PRIMARY, error="404")
    health.advance_model("run-alice", reason="404", from_model=PRIMARY)

    data = health_client.get("/api/provider-health?run_id=run-alice").json()["data"]
    assert data["run_id"] != "run-alice"
    assert data["run_id"] == "run-bob"
    assert data["chain_position"] == 0
