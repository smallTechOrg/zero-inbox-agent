"""Unit tests for GET /api/digest/latest."""

from __future__ import annotations

import pytest


class TestDigestLatest:
    def test_digest_happy_path(self, client, sign_in, seed):
        sign_in("user-alice")
        resp = client.get("/api/digest/latest")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["run_id"] == "run-alice"
        assert "generated_at" in data
        assert isinstance(data["time_sensitive_kept"], list)
        assert isinstance(data["vip_mail"], list)
        assert isinstance(data["needs_your_call"], list)
        assert "auto_archived" in data
        assert "count" in data["auto_archived"]
        assert "by_category" in data["auto_archived"]

    def test_digest_needs_your_call_included(self, client, sign_in, seed):
        """dec-alice-2 has status=needs_your_call — it must appear in the digest."""
        sign_in("user-alice")
        resp = client.get("/api/digest/latest")
        data = resp.json()["data"]
        assert len(data["needs_your_call"]) >= 1
        entry = data["needs_your_call"][0]
        assert "subject" in entry
        assert "from" in entry
        assert "reasoning" in entry

    def test_digest_404_no_completed_run(self, client, sign_in, seed, db):
        """If no completed run exists for the user, return 404."""
        from db.models import TriageRun

        # Set alice's run to 'running' so no completed run exists
        run = db.get(TriageRun, "run-alice")
        run.status = "running"
        db.commit()

        sign_in("user-alice")
        resp = client.get("/api/digest/latest")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    def test_digest_scoped_to_user(self, client, sign_in, seed):
        """Alice's digest must not include Bob's run_id."""
        sign_in("user-alice")
        resp = client.get("/api/digest/latest")
        data = resp.json()["data"]
        assert data["run_id"] == "run-alice"
        assert "run-bob" not in str(data)

    def test_digest_requires_auth(self, client, seed):
        resp = client.get("/api/digest/latest")
        assert resp.status_code == 401

    def test_digest_time_sensitive_kept_shape(self, client, sign_in, seed, db):
        """Add a time_sensitive kept decision and verify it appears."""
        from datetime import datetime, timezone

        from db.models import Decision, Item

        # dec-alice-0 is proposed_action='keep' — set it time_sensitive=True
        decision = db.get(Decision, "dec-alice-0")
        decision.time_sensitive = True
        db.commit()

        sign_in("user-alice")
        resp = client.get("/api/digest/latest")
        data = resp.json()["data"]
        ts = data["time_sensitive_kept"]
        assert len(ts) >= 1
        assert "subject" in ts[0]
        assert "from" in ts[0]
        assert "reason" in ts[0]
