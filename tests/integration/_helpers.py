"""Shared helpers for the Phase-1 integration seam tests."""

from __future__ import annotations

import time

import pytest


def envelope_ok(response) -> dict:
    """Assert the spec/api.md success envelope and return ``data``."""
    body = response.json()
    assert body.get("ok") is True, f"expected ok envelope, got {response.status_code}: {body}"
    assert "data" in body
    return body["data"]


def envelope_error(response, code: str | None = None) -> dict:
    """Assert the spec/api.md error envelope and return ``error``."""
    body = response.json()
    assert body.get("ok") is False, f"expected error envelope, got: {body}"
    error = body.get("error") or {}
    assert error.get("code"), f"error envelope must carry a code: {body}"
    assert error.get("message"), f"error envelope must carry a message: {body}"
    if code is not None:
        assert error["code"] == code, f"expected code={code!r}, got {error}"
    return error


def start_run(client, chunk_limit: int | None = None) -> str:
    payload = {} if chunk_limit is None else {"chunk_limit": chunk_limit}
    response = client.post("/api/runs", json=payload)
    data = envelope_ok(response)
    run_id = data.get("run_id") or data.get("id")
    assert run_id, f"POST /api/runs must return a run_id: {data}"
    return run_id


def wait_for_run(client, run_id: str, timeout: float = 180.0) -> dict:
    """Poll GET /api/runs/{id} until the run leaves ``running``."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        data = envelope_ok(client.get(f"/api/runs/{run_id}"))
        run = data.get("run", data)
        last = run
        if run.get("status") != "running":
            return run
        time.sleep(1.0)
    pytest.fail(f"run {run_id} still 'running' after {timeout}s: {last}")


def run_to_completion(client, chunk_limit: int | None = None, timeout: float = 180.0) -> tuple[str, dict]:
    run_id = start_run(client, chunk_limit)
    run = wait_for_run(client, run_id, timeout)
    return run_id, run
