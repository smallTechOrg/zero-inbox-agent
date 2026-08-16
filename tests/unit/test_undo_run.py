"""Unit tests — runs-undo-api slice: whole-run undo (tools/undo.py + the route).

The Gmail write API is never touched: ``AGENT_GMAIL_WRITE_DISABLED=1`` makes
the choke point record mutations as applied without a network call, and the
suite's network backstop would refuse any write regardless.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import seed_user
from tests.unit.test_runs_api import ALICE, BOB, make_client


@pytest.fixture
def alice(db_session):
    user, categories = seed_user(db_session, user_id=ALICE, email="alice@example.com")
    # Labels created lazily by the graph; give the ones undo must resolve.
    categories["Newsletters"].gmail_label_id = "test-label-newsletters"
    categories["Finance"].gmail_label_id = "test-label-finance"
    db_session.commit()
    return user, categories


class RecordingMutator:
    """Same surface as GmailMutator via inheritance, recording call order."""

    def __init__(self):
        from channels.gmail.mutations import GmailMutator

        self._inner = GmailMutator(None)
        self.calls: list[tuple[str, str, str | None]] = []

    def apply_inverse(self, op, thread_id, *, label_id=None, reason=""):
        result = self._inner.apply_inverse(op, thread_id, label_id=label_id, reason=reason)
        self.calls.append((result["op"], thread_id, label_id))
        assert result["simulated"] is True  # the guard held — no Gmail write
        return result


@pytest.fixture
def client(alice, monkeypatch):
    monkeypatch.setenv("AGENT_GMAIL_WRITE_DISABLED", "1")
    import api.runs as runs_module

    mutator = RecordingMutator()
    monkeypatch.setattr(
        runs_module, "gmail_mutator_for_user", lambda session, user_id: mutator
    )
    with make_client(ALICE) as c:
        c.mutator = mutator
        yield c


def _seed_run_with_mutations(db_session, categories, *, user_id=ALICE, status="completed"):
    """A completed run that filed one thread to Newsletters and archived it."""
    from db.models import Mutation, Run, ThreadDecision

    now = datetime.now(timezone.utc)
    run = Run(user_id=user_id, status=status, started_at=now - timedelta(minutes=2), finished_at=now)
    db_session.add(run)
    db_session.flush()
    thread_id = f"test-thread-{run.id[:8]}"
    db_session.add(
        ThreadDecision(
            user_id=user_id,
            run_id=run.id,
            gmail_thread_id=thread_id,
            sender="news@example.com",
            subject="Weekly digest",
            category_id=categories["Newsletters"].id,
            confidence=0.95,
            reason="newsletter",
        )
    )
    label = Mutation(
        user_id=user_id,
        run_id=run.id,
        gmail_thread_id=thread_id,
        action="add_label",
        label_name="Newsletters",
        reason="filed to Newsletters",
        applied_at=now - timedelta(seconds=20),
    )
    archive = Mutation(
        user_id=user_id,
        run_id=run.id,
        gmail_thread_id=thread_id,
        action="remove_inbox",
        label_name=None,
        reason="Newsletters is label_and_archive",
        applied_at=now - timedelta(seconds=10),
    )
    db_session.add_all([label, archive])
    db_session.commit()
    return run, label, archive


def test_undo_replays_audit_rows_in_reverse_and_restores_state(client, db_session, alice):
    _, categories = alice
    run, label, archive = _seed_run_with_mutations(db_session, categories)

    resp = client.post(f"/api/runs/{run.id}/undo")
    assert resp.status_code == 200, resp.text
    # Async contract: the route accepts and the TestClient runs the background
    # task to completion before returning — final state is asserted below.
    assert resp.json()["data"] == {"run_id": run.id, "undo_started": True}

    # Newest first: the archive is inverted before the label.
    thread_id = f"test-thread-{run.id[:8]}"
    assert client.mutator.calls == [
        ("restore_inbox", thread_id, None),
        ("remove_label", thread_id, "test-label-newsletters"),
    ]

    db_session.expire_all()
    from db.models import Mutation, Run, ThreadDecision

    assert all(
        m.undone_at is not None
        for m in db_session.query(Mutation).filter_by(run_id=run.id)
    )
    assert db_session.get(Run, run.id).status == "undone"
    assert db_session.get(Run, run.id).undone_at is not None
    # Undone threads become re-decidable.
    decision = db_session.query(ThreadDecision).filter_by(run_id=run.id).one()
    assert decision.undone is True

    # The undo audited itself onto the feed: started, one per mutation, finished.
    from db.models import RunEvent

    events = (
        db_session.query(RunEvent).filter_by(run_id=run.id).order_by(RunEvent.seq).all()
    )
    assert [e.type for e in events] == [
        "undo_started", "undo_action", "undo_action", "undo_finished",
    ]
    assert [e.seq for e in events] == [1, 2, 3, 4]
    assert "restoring 2 Gmail changes" in events[0].sentence


def test_undo_is_refused_while_running_and_after_undone(client, db_session, alice):
    _, categories = alice
    run, *_ = _seed_run_with_mutations(db_session, categories, status="running")
    resp = client.post(f"/api/runs/{run.id}/undo")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"

    done, *_ = _seed_run_with_mutations(db_session, categories)
    assert client.post(f"/api/runs/{done.id}/undo").status_code == 200
    again = client.post(f"/api/runs/{done.id}/undo")
    assert again.status_code == 409
    assert "already been undone" in again.json()["error"]["message"]


def test_interrupted_undo_resumes_without_double_inverting(client, db_session, alice):
    _, categories = alice
    run, label, archive = _seed_run_with_mutations(db_session, categories)
    # First attempt already undid the newest row (the archive).
    archive.undone_at = datetime.now(timezone.utc)
    db_session.commit()

    resp = client.post(f"/api/runs/{run.id}/undo")
    assert resp.status_code == 200
    # Only the remaining (label) row was inverted — nothing double-inverted.
    assert client.mutator.calls == [
        ("remove_label", f"test-thread-{run.id[:8]}", "test-label-newsletters")
    ]


def test_undo_of_foreign_run_is_404(client, db_session):
    _, bob_cats = seed_user(db_session, user_id=BOB, email="bob@example.com")
    bob_run, *_ = _seed_run_with_mutations(db_session, bob_cats, user_id=BOB)
    assert client.post(f"/api/runs/{bob_run.id}/undo").status_code == 404


def test_revoked_token_surfaces_as_gmail_reconnect(alice, db_session, monkeypatch):
    """Error path: ReauthRequired mid-undo → structured reconnect, progress kept."""
    monkeypatch.setenv("AGENT_GMAIL_WRITE_DISABLED", "1")
    _, categories = alice
    run, *_ = _seed_run_with_mutations(db_session, categories)

    from channels.base import ReauthRequired

    class RevokedMutator:
        def apply_inverse(self, *args, **kwargs):
            raise ReauthRequired("Gmail rejected the stored credentials")

    import api.runs as runs_module

    monkeypatch.setattr(
        runs_module, "gmail_mutator_for_user", lambda session, user_id: RevokedMutator()
    )
    with make_client(ALICE) as client:
        resp = client.post(f"/api/runs/{run.id}/undo")
    # Async contract: the route accepts (the account looks connected at POST
    # time); the background task hits ReauthRequired and flips the account to
    # needs_reconnect — the UI's /api/me poll surfaces the reconnect prompt.
    assert resp.status_code == 200
    assert "Traceback" not in resp.text

    db_session.expire_all()
    from db.models import GmailAccount, Run

    assert db_session.get(Run, run.id).status == "completed"  # NOT marked undone
    account = db_session.query(GmailAccount).filter_by(user_id=ALICE).one()
    assert account.status == "needs_reconnect"


def test_undo_with_unresolvable_label_reports_error_and_continues(client, db_session, alice):
    """Edge: a label with no known Gmail id is reported, the rest still undoes."""
    _, categories = alice
    run, label, archive = _seed_run_with_mutations(db_session, categories)
    label.label_name = "Ghost category"  # no gmail_label_id anywhere
    db_session.commit()

    resp = client.post(f"/api/runs/{run.id}/undo")
    assert resp.status_code == 200

    db_session.expire_all()
    from db.models import Mutation, Run

    # The archive was still restored; the ghost-label row stayed pending.
    undone = [m for m in db_session.query(Mutation).filter_by(run_id=run.id) if m.undone_at]
    assert len(undone) == 1
    # Incomplete undo never claims completion.
    assert db_session.get(Run, run.id).status != "undone"
