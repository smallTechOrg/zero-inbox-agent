"""Unit tests for Phase 3 run endpoints: summary and approve-and-apply."""

from __future__ import annotations

import pytest


class TestRunSummary:
    def test_summary_happy_path(self, client, sign_in, seed):
        sign_in("user-alice")
        resp = client.get("/api/runs/run-alice/summary")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["run_id"] == "run-alice"
        assert data["status"] == "completed"
        assert isinstance(data["total_threads"], int)
        assert isinstance(data["categories"], list)
        assert isinstance(data["top_clusters"], list)
        assert isinstance(data["needs_your_call_count"], int)
        assert isinstance(data["cost_usd"], float)

    def test_summary_categories_populated(self, client, sign_in, seed):
        sign_in("user-alice")
        resp = client.get("/api/runs/run-alice/summary")
        data = resp.json()["data"]
        # Alice has decisions with category_id=cat-alice; at least one category should appear
        assert len(data["categories"]) >= 1
        cat = data["categories"][0]
        assert "name" in cat
        assert "count" in cat
        assert "suggested_action" in cat

    def test_summary_top_clusters(self, client, sign_in, seed):
        sign_in("user-alice")
        resp = client.get("/api/runs/run-alice/summary")
        data = resp.json()["data"]
        clusters = data["top_clusters"]
        assert len(clusters) <= 3
        if clusters:
            assert "label" in clusters[0]
            assert "count" in clusters[0]
            assert "suggested_action" in clusters[0]

    def test_summary_404_unknown_run(self, client, sign_in, seed):
        sign_in("user-alice")
        resp = client.get("/api/runs/nonexistent-run/summary")
        assert resp.status_code == 404

    def test_summary_scoped_to_user(self, client, sign_in, seed):
        """Alice cannot see Bob's run."""
        sign_in("user-alice")
        resp = client.get("/api/runs/run-bob/summary")
        assert resp.status_code == 404

    def test_summary_requires_auth(self, client, seed):
        resp = client.get("/api/runs/run-alice/summary")
        assert resp.status_code == 401


# TestApproveAndApply removed — autonomous triage mode replaces the
# manual approve-and-apply flow. The POST /api/runs/{run_id}/undo endpoint
# is tested in tests/unit/api/test_runs_undo.py.
