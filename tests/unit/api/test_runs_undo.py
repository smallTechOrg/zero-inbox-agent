"""Unit tests for POST /api/runs/{run_id}/undo — run-level undo.

Three required scenarios per capability:
  1. Happy path — n decisions reversed, ActionLog.undone_at set, decisions → "undone"
  2. Idempotent — second call returns same result without extra Gmail calls
  3. Error paths — 404 for unknown run, 422 for non-completed run, scope isolation
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_action_logs(db, user_id: str, run_id: str, decision_ids: list[str]) -> list:
    """Insert ActionLog rows linked to the given decisions."""
    from db.models import ActionLog

    logs = []
    for dec_id in decision_ids:
        log = ActionLog(
            user_id=user_id,
            decision_id=dec_id,
            operation="archive",
            request_params={"thread_id": f"thread-for-{dec_id}"},
            undo_token={
                "thread_id": f"thread-for-{dec_id}",
                "category_label_id": "Label_newsletters",
                "original_label_ids": ["INBOX", "Label_old"],
                "labels_added_by_triage": ["Label_newsletters"],
            },
        )
        db.add(log)
        logs.append(log)
    db.commit()
    # Re-read to get generated ids
    db.expire_all()
    return logs


def _fake_mutator():
    m = MagicMock()
    m.restore_labels.return_value = {"thread_id": "t", "label_ids": ["INBOX"]}
    m.undo_archive_and_label.return_value = {"thread_id": "t", "label_ids": ["INBOX"]}
    return m


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRunUndo:
    def test_happy_path_reverses_all_actions(self, client, sign_in, seed, db):
        """Undo a completed run: all action logs are reversed, decisions become undone."""
        from db.models import ActionLog, Decision

        # Pre-approve some decisions so apply_decision flow has something to undo
        db.get(Decision, "dec-alice-1").status = "applied"
        db.commit()

        logs = _seed_action_logs(db, "user-alice", "run-alice", ["dec-alice-1"])
        log_id = logs[0].id

        fake_mutator = _fake_mutator()
        fake_label = MagicMock()

        with patch("api.actions._mutator_and_labels_for_user", return_value=(fake_mutator, fake_label)):
            sign_in("user-alice")
            resp = client.post("/api/runs/run-alice/undo")

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["reversed"] == 1
        assert data["skipped"] == 0
        assert data["errors"] == []

        db.expire_all()
        log = db.get(ActionLog, log_id)
        assert log.undone_at is not None
        decision = db.get(Decision, "dec-alice-1")
        assert decision.status == "undone"

    def test_idempotent_second_call_skips_already_undone(self, client, sign_in, seed, db):
        """A second undo call returns the same result; Gmail is NOT called again."""
        from db.models import Decision

        db.get(Decision, "dec-alice-1").status = "applied"
        db.commit()

        _seed_action_logs(db, "user-alice", "run-alice", ["dec-alice-1"])

        fake_mutator = _fake_mutator()
        fake_label = MagicMock()

        with patch("api.actions._mutator_and_labels_for_user", return_value=(fake_mutator, fake_label)):
            sign_in("user-alice")
            first = client.post("/api/runs/run-alice/undo")
            second = client.post("/api/runs/run-alice/undo")

        assert first.status_code == 200
        assert second.status_code == 200
        first_data = first.json()["data"]
        second_data = second.json()["data"]
        assert first_data["reversed"] == 1
        # Second call: everything already undone → skipped
        assert second_data["reversed"] == 0
        assert second_data["skipped"] == 1
        # Gmail restore called exactly once
        assert fake_mutator.restore_labels.call_count == 1

    def test_unknown_run_returns_404(self, client, sign_in, seed):
        sign_in("user-alice")
        resp = client.post("/api/runs/nonexistent-run/undo")
        assert resp.status_code == 404

    def test_cannot_undo_another_users_run(self, client, sign_in, seed, db):
        """Scope isolation: Alice cannot undo Bob's run."""
        from db.models import Decision

        db.get(Decision, "dec-bob-1").status = "applied"
        db.commit()

        _seed_action_logs(db, "user-bob", "run-bob", ["dec-bob-1"])

        fake_mutator = _fake_mutator()
        fake_label = MagicMock()

        with patch("api.actions._mutator_and_labels_for_user", return_value=(fake_mutator, fake_label)):
            sign_in("user-alice")
            resp = client.post("/api/runs/run-bob/undo")

        assert resp.status_code == 404
        # Bob's action log must be untouched
        db.expire_all()
        bob_decision = db.get(Decision, "dec-bob-1")
        assert bob_decision.status == "applied"

    def test_cannot_undo_a_running_run(self, client, sign_in, seed, db):
        """Only completed runs may be undone."""
        from db.models import TriageRun
        from uuid import uuid4

        running_run = TriageRun(
            id="run-alice-running",
            user_id="user-alice",
            channel_account_id=seed["alice"]["account"].id,
            status="running",
            dry_run=False,
            items_total=10,
            items_decided=0,
            counts={},
        )
        db.add(running_run)
        db.commit()

        sign_in("user-alice")
        resp = client.post("/api/runs/run-alice-running/undo")
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"

    def test_run_with_no_action_logs_returns_zero(self, client, sign_in, seed):
        """A completed run with no applied actions returns reversed=0 cleanly."""
        fake_mutator = _fake_mutator()
        fake_label = MagicMock()

        with patch("api.actions._mutator_and_labels_for_user", return_value=(fake_mutator, fake_label)):
            sign_in("user-alice")
            resp = client.post("/api/runs/run-alice/undo")

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["reversed"] == 0
        assert data["skipped"] == 0

    def test_undo_requires_auth(self, client, seed):
        resp = client.post("/api/runs/run-alice/undo")
        assert resp.status_code == 401

    def test_legacy_token_without_original_label_ids(self, client, sign_in, seed, db):
        """Undo token without original_label_ids falls back to category_label_id path."""
        from db.models import ActionLog, Decision

        db.get(Decision, "dec-alice-1").status = "applied"
        db.commit()

        log = ActionLog(
            user_id="user-alice",
            decision_id="dec-alice-1",
            operation="archive",
            request_params={"thread_id": "thread-for-dec-alice-1"},
            undo_token={
                "thread_id": "thread-for-dec-alice-1",
                "category_label_id": "Label_newsletters",
                # No original_label_ids — legacy token format
            },
        )
        db.add(log)
        db.commit()

        fake_mutator = _fake_mutator()
        fake_label = MagicMock()

        with patch("api.actions._mutator_and_labels_for_user", return_value=(fake_mutator, fake_label)):
            sign_in("user-alice")
            resp = client.post("/api/runs/run-alice/undo")

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["reversed"] == 1
        # Legacy path calls undo_archive_and_label, not restore_labels
        fake_mutator.undo_archive_and_label.assert_called_once_with(
            "thread-for-dec-alice-1", category_label_id="Label_newsletters"
        )
