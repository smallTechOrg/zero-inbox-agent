"""GET /api/runs/{id}/remainder and POST /api/runs/{id}/apply.

Happy path (the envelope and the ledger), edge cases (dry-run, a run with no
decisions, the run/summary payload additions) and error paths (another user's run
is a 404, a running run is a 409 `not_appliable`).
"""

from __future__ import annotations

import pytest


@pytest.fixture
def apply_calls(monkeypatch):
    """Record every call to the (slice 2) apply pass instead of touching Gmail."""
    calls: list[dict] = []
    applied_once = {"done": False}

    def fake_apply_run_decisions(*, run_id, user_id, channel_account_id, dry_run):
        calls.append(
            {
                "run_id": run_id,
                "user_id": user_id,
                "channel_account_id": channel_account_id,
                "dry_run": dry_run,
            }
        )
        if applied_once["done"]:
            # Idempotent second pass: nothing left to do.
            return {"applied": 0, "already_applied": 2, "failed": 0, "failures": [], "dry_run": dry_run}
        applied_once["done"] = True
        return {"applied": 2, "already_applied": 0, "failed": 0, "failures": [], "dry_run": dry_run}

    monkeypatch.setattr("graph.nodes.apply_run_decisions", fake_apply_run_decisions, raising=False)
    import api.runs as runs_module

    runs_module._apply_in_flight.clear()
    yield calls
    runs_module._apply_in_flight.clear()


def _set_autonomy(db, run_id, mapping: dict[str, tuple[str | None, str]]):
    """Assign (autonomy_state, status) to the seeded decisions of *run_id*."""
    from db.models import Decision

    rows = db.execute(
        __import__("sqlalchemy").select(Decision).where(Decision.run_id == run_id).order_by(Decision.id)
    ).scalars().all()
    for row, key in zip(rows, sorted(mapping)):
        row.autonomy_state, row.status = mapping[key]
    db.commit()


class TestRemainderEndpoint:
    def test_it_returns_the_full_ledger_in_the_standard_envelope(self, client, sign_in, seed, db):
        sign_in("user-alice")
        _set_autonomy(
            db,
            "run-alice",
            {"a": ("auto_act", "applied"), "b": ("category_keep", "proposed"), "c": ("needs_your_call", "needs_your_call")},
        )

        response = client.get("/api/runs/run-alice/remainder")

        assert response.status_code == 200
        body = response.json()
        assert body["error"] is None
        data = body["data"]
        assert data["run_id"] == "run-alice"
        assert data["applied"] == 1
        assert data["distance_to_zero"] == 0
        assert data["apply_ok"] is True
        assert data["apply_failed_reason"] is None
        assert data["dry_run"] is True  # the seeded run is a dry run
        assert data["remainder"] == {
            "needs_your_call": 1,
            "category_keep": 1,
            "held_by_never_miss": 0,
            "below_threshold": 0,
            "unclassified": 0,
        }
        assert data["inbox_remaining"] == sum(data["remainder"].values()) + data["distance_to_zero"]
        assert data["failures"] == []

    def test_unapplied_auto_act_rows_make_the_run_read_as_not_ok(self, client, sign_in, seed, db):
        sign_in("user-alice")
        _set_autonomy(
            db,
            "run-alice",
            {"a": ("auto_act", "proposed"), "b": ("auto_act", "proposed"), "c": ("category_keep", "proposed")},
        )

        data = client.get("/api/runs/run-alice/remainder").json()["data"]

        assert data["distance_to_zero"] == 2
        assert data["apply_ok"] is False

    def test_pre_phase_7_decisions_are_reported_as_unclassified(self, client, sign_in, seed):
        sign_in("user-alice")  # the seeded rows have autonomy_state NULL

        data = client.get("/api/runs/run-alice/remainder").json()["data"]

        assert data["remainder"]["unclassified"] == 3
        assert data["inbox_remaining"] == 3

    def test_another_users_run_is_a_404_not_a_leak(self, client, sign_in, seed):
        sign_in("user-bob")

        response = client.get("/api/runs/run-alice/remainder")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    def test_it_requires_a_session(self, client, seed):
        assert client.get("/api/runs/run-alice/remainder").status_code == 401

    def test_an_unknown_run_id_is_a_404(self, client, sign_in, seed):
        sign_in("user-alice")

        assert client.get("/api/runs/nope/remainder").status_code == 404


class TestRunAndSummaryPayloads:
    def test_the_run_payload_carries_distance_to_zero_and_apply_ok(self, client, sign_in, seed, db):
        sign_in("user-alice")
        _set_autonomy(
            db,
            "run-alice",
            {"a": ("auto_act", "proposed"), "b": ("category_keep", "proposed"), "c": ("category_keep", "proposed")},
        )

        data = client.get("/api/runs/run-alice").json()["data"]

        assert data["distance_to_zero"] == 1
        assert data["apply_ok"] is False

    def test_latest_run_carries_them_too(self, client, sign_in, seed):
        sign_in("user-alice")

        data = client.get("/api/runs/latest").json()["data"]

        assert "distance_to_zero" in data and "apply_ok" in data

    def test_the_summary_gains_applied_count_distance_and_remainder(self, client, sign_in, seed, db):
        sign_in("user-alice")
        _set_autonomy(
            db,
            "run-alice",
            {"a": ("auto_act", "applied"), "b": ("auto_act", "proposed"), "c": ("below_threshold", "proposed")},
        )

        data = client.get("/api/runs/run-alice/summary").json()["data"]

        assert data["applied_count"] == 1
        assert data["distance_to_zero"] == 1
        assert data["remainder"]["below_threshold"] == 1


class TestApplyEndpoint:
    def test_it_queues_the_apply_pass_and_returns_the_ledger(self, client, sign_in, seed, db, apply_calls):
        sign_in("user-alice")
        _set_autonomy(
            db,
            "run-alice",
            {"a": ("auto_act", "proposed"), "b": ("auto_act", "proposed"), "c": ("category_keep", "proposed")},
        )

        response = client.post("/api/runs/run-alice/apply")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["queued"] is True
        assert data["distance_to_zero"] == 2
        assert data["apply_ok"] is False
        assert apply_calls == [
            {
                "run_id": "run-alice",
                "user_id": "user-alice",
                "channel_account_id": "conn-alice",
                "dry_run": True,
            }
        ]

    def test_calling_it_twice_is_a_no_op_the_second_time(self, client, sign_in, seed, db, apply_calls):
        sign_in("user-alice")
        _set_autonomy(
            db,
            "run-alice",
            {"a": ("auto_act", "applied"), "b": ("auto_act", "applied"), "c": ("category_keep", "proposed")},
        )

        first = client.post("/api/runs/run-alice/apply")
        second = client.post("/api/runs/run-alice/apply")

        assert first.status_code == second.status_code == 200
        # Both calls reach the idempotent pass; the second one applies nothing.
        assert len(apply_calls) == 2
        assert second.json()["data"]["distance_to_zero"] == 0
        assert second.json()["data"]["apply_ok"] is True

    def test_a_running_run_is_rejected_with_not_appliable(self, client, sign_in, seed, db, apply_calls):
        from db.models import TriageRun

        sign_in("user-alice")
        db.get(TriageRun, "run-alice").status = "running"
        db.commit()

        response = client.post("/api/runs/run-alice/apply")

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "not_appliable"
        assert apply_calls == []

    def test_another_users_run_is_a_404_and_is_never_applied(self, client, sign_in, seed, apply_calls):
        sign_in("user-bob")

        response = client.post("/api/runs/run-alice/apply")

        assert response.status_code == 404
        assert apply_calls == []

    def test_a_failing_apply_pass_never_breaks_the_request(self, client, sign_in, seed, monkeypatch, apply_calls):
        def boom(**_kwargs):
            raise RuntimeError("gmail is down")

        monkeypatch.setattr("graph.nodes.apply_run_decisions", boom, raising=False)
        sign_in("user-alice")

        response = client.post("/api/runs/run-alice/apply")

        assert response.status_code == 200
        # and the in-flight guard is released, so a retry is still possible
        import api.runs as runs_module

        assert "run-alice" not in runs_module._apply_in_flight
