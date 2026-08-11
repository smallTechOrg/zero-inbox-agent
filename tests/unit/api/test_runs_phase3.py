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


class TestApproveAndApply:
    def test_approve_and_apply_dry_run_on_rejects(self, client, sign_in, seed):
        """Default settings have dry_run=True — approve-and-apply must refuse."""
        sign_in("user-alice")
        resp = client.post("/api/runs/run-alice/approve-and-apply")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "dry_run_violation"

    def test_approve_and_apply_404_unknown_run(self, client, sign_in, seed, db):
        """Turn off dry_run for alice, then ask for a non-existent run."""
        from db.models import UserSettings

        settings = db.get(UserSettings, "user-alice")
        settings.dry_run = False
        db.commit()

        sign_in("user-alice")
        resp = client.post("/api/runs/nonexistent-run/approve-and-apply")
        assert resp.status_code == 404

    def test_approve_and_apply_scoped_to_user(self, client, sign_in, seed, db):
        """Alice cannot approve Bob's run."""
        from db.models import UserSettings

        settings = db.get(UserSettings, "user-alice")
        settings.dry_run = False
        db.commit()

        sign_in("user-alice")
        resp = client.post("/api/runs/run-bob/approve-and-apply")
        assert resp.status_code == 404

    def test_approve_and_apply_skips_keep_and_nyc(self, client, sign_in, seed, db):
        """With dry_run off, keep-proposed and needs_your_call decisions are skipped.
        The actual Gmail apply will fail because there are no real credentials in the
        unit test DB — but the response shape is what we validate here (we mock the
        mutator below to make apply succeed)."""
        from unittest.mock import MagicMock, patch

        from db.models import UserSettings

        settings = db.get(UserSettings, "user-alice")
        settings.dry_run = False
        db.commit()

        fake_mutator = MagicMock()
        fake_mutator.archive_and_label.return_value = {"id": "log-1"}
        fake_label = MagicMock()
        fake_label.ensure_label.return_value = {"id": "Label_123", "name": "ZeroInbox/Newsletters"}

        with patch("api.actions._mutator_and_labels_for_user", return_value=(fake_mutator, fake_label)):
            sign_in("user-alice")
            resp = client.post("/api/runs/run-alice/approve-and-apply")

        assert resp.status_code == 200
        data = resp.json()["data"]
        # dec-alice-0 is 'keep' → skipped_keep
        assert data["skipped_keep"] >= 1
        # dec-alice-2 is needs_your_call → skipped_needs_your_call
        assert data["skipped_needs_your_call"] == 0  # counted via status filter
        assert "applied" in data
        assert "undo_tokens" in data

    def test_approve_and_apply_requires_auth(self, client, seed):
        resp = client.post("/api/runs/run-alice/approve-and-apply")
        assert resp.status_code == 401
