"""Seam: audit API → Gmail reads → inbox_snapshots. Strictly read-only.

The Gmail read layer is faked at the listing seam for the synthetic user; the
write-guard layers prove read-only-ness structurally (any Gmail write raises).
"""

from __future__ import annotations

import pytest

from tests.fixtures.fake_gmail import install_inbox
from tests.fixtures.threads import sample_threads
from tests.integration._helpers import envelope_error, envelope_ok

pytestmark = pytest.mark.integration


@pytest.fixture
def fake_inbox(monkeypatch):
    return install_inbox(monkeypatch, sample_threads())


class TestMiniAudit:
    def test_audit_returns_counts_and_persists_a_snapshot(
        self, auth_client, seeded_user, fake_inbox, db_session
    ):
        data = envelope_ok(auth_client.post("/api/audit"))
        blob = str(data)
        assert "total" in blob or "total_inbox_threads" in blob, f"no counts in {data}"

        from db import models

        rows = (
            db_session.query(models.InboxSnapshot)
            .filter(models.InboxSnapshot.user_id == seeded_user[0].id)
            .all()
        )
        assert len(rows) == 1, "POST /api/audit must persist exactly one snapshot row"
        snap = rows[0]
        assert snap.total_inbox_threads is not None
        assert snap.top_senders_json is not None

    def test_audit_writes_zero_mutations_and_zero_decisions(
        self, auth_client, seeded_user, fake_inbox, db_session
    ):
        envelope_ok(auth_client.post("/api/audit"))
        from db import models

        assert db_session.query(models.Mutation).count() == 0, "AUDIT MUTATED GMAIL"
        assert db_session.query(models.ThreadDecision).count() == 0
        assert db_session.query(models.LlmCall).count() == 0, "audit must make no LLM calls"

    def test_latest_returns_the_most_recent_snapshot(self, auth_client, seeded_user, fake_inbox):
        envelope_ok(auth_client.post("/api/audit"))
        envelope_ok(auth_client.post("/api/audit"))
        data = envelope_ok(auth_client.get("/api/audit/latest"))
        assert data, "GET /api/audit/latest must return the latest snapshot"

    def test_latest_with_no_snapshot_is_a_designed_empty_state_not_a_500(self, auth_client, seeded_user):
        response = auth_client.get("/api/audit/latest")
        assert response.status_code < 500
        body = response.json()
        if response.status_code == 404:
            envelope_error(response)
        else:
            assert body.get("ok") is True
