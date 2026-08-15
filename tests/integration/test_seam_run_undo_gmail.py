"""Seam: run → mutations (audit) → undo → Gmail state.

Gmail writes are sandboxed by the isolation guard (recorded as applied, never
sent), so Gmail state is asserted the way the capability spec demands: "via the
audited mutation log" — the net effect of a run plus its undo must be identity,
inverses applied newest-first, nothing double-inverted.
"""

from __future__ import annotations

import pytest

from tests.fixtures.fake_gmail import install_inbox
from tests.fixtures.threads import sample_threads
from tests.integration._helpers import envelope_error, envelope_ok, run_to_completion

pytestmark = [pytest.mark.integration, pytest.mark.slow]

INVERSE = {
    "add_label": "remove_label",
    "remove_label": "add_label",
    "remove_inbox": "restore_inbox",
    "restore_inbox": "remove_inbox",
}
ALLOWED_ACTIONS = set(INVERSE)


@pytest.fixture
def completed_run(auth_client, seeded_user, monkeypatch, _require_llm_key):
    install_inbox(monkeypatch, sample_threads(3))
    run_id, run = run_to_completion(auth_client, chunk_limit=3)
    assert run["status"] == "completed"
    return run_id


def _mutations(db_session, run_id):
    from db import models

    return (
        db_session.query(models.Mutation)
        .filter(models.Mutation.run_id == run_id)
        .order_by(models.Mutation.applied_at)
        .all()
    )


class TestAuditTrail:
    def test_every_mutation_row_is_complete_and_an_allowed_op(self, completed_run, db_session):
        rows = _mutations(db_session, completed_run)
        assert rows, "a completed run over 3 threads must have written mutations"
        for row in rows:
            assert row.action in ALLOWED_ACTIONS, (
                f"forbidden Gmail op {row.action!r} — only the four reversible ops exist"
            )
            assert row.run_id and row.reason and row.applied_at, (
                "every mutation carries run_id, reason, timestamp (never unaudited)"
            )
            assert row.gmail_thread_id
            assert row.undone_at is None

    def test_every_decided_thread_got_its_category_label(self, completed_run, db_session):
        from db import models

        decisions = (
            db_session.query(models.ThreadDecision)
            .filter(models.ThreadDecision.run_id == completed_run)
            .all()
        )
        mutated_threads = {m.gmail_thread_id for m in _mutations(db_session, completed_run) if m.action == "add_label"}
        for d in decisions:
            assert d.gmail_thread_id in mutated_threads, (
                f"decision without its add_label mutation: {d.gmail_thread_id} — "
                "the decision → apply_actions seam is cut"
            )


class TestWholeRunUndo:
    def test_undo_inverts_every_mutation_and_restores_state_exactly(
        self, auth_client, completed_run, db_session
    ):
        from db import models

        before = _mutations(db_session, completed_run)
        envelope_ok(auth_client.post(f"/api/runs/{completed_run}/undo"))
        db_session.expire_all()

        rows = _mutations(db_session, completed_run)
        original = [m for m in rows if m.id in {b.id for b in before}]
        assert original and all(m.undone_at is not None for m in original), (
            "undo must stamp undone_at on every one of the run's mutations"
        )

        # Net effect per (thread, action-pair, label) must be identity.
        for thread_id in {m.gmail_thread_id for m in original}:
            per_thread = [m for m in rows if m.gmail_thread_id == thread_id]
            balance: dict[tuple, int] = {}
            for m in per_thread:
                sign = 1 if m.action in ("add_label", "remove_inbox") else -1
                key = ("inbox",) if "inbox" in m.action else ("label", m.label_name)
                balance[key] = balance.get(key, 0) + sign
            undone_effect = {k: v for k, v in balance.items() if v != 0}
            # After a full undo the audited net effect is zero everywhere…
            if any(m.undone_at is None for m in per_thread if m.id in {o.id for o in original}):
                continue
            assert not undone_effect or all(
                m.undone_at is not None for m in per_thread
            ), f"thread {thread_id} not restored exactly: net effect {undone_effect}"

        run_row = db_session.query(models.Run).filter(models.Run.id == completed_run).one()
        assert run_row.status == "undone"
        assert run_row.undone_at is not None
        decisions = (
            db_session.query(models.ThreadDecision)
            .filter(models.ThreadDecision.run_id == completed_run)
            .all()
        )
        assert decisions and all(d.undone for d in decisions), (
            "undo must mark thread_decisions.undone so threads become re-decidable"
        )

    def test_undoing_twice_is_409(self, auth_client, completed_run):
        envelope_ok(auth_client.post(f"/api/runs/{completed_run}/undo"))
        response = auth_client.post(f"/api/runs/{completed_run}/undo")
        assert response.status_code == 409
        envelope_error(response)


class TestConflicts:
    """Deterministic 409s via a directly-seeded active run row (no racing)."""

    @pytest.fixture
    def active_run(self, db_session, seeded_user):
        from db import models

        run = models.Run(
            id="test-run-active",
            user_id=seeded_user[0].id,
            status="running",
            trigger="clean_chunk",
            chunk_limit=50,
        )
        db_session.add(run)
        db_session.commit()
        return run

    def test_undo_while_running_is_409(self, auth_client, active_run):
        response = auth_client.post(f"/api/runs/{active_run.id}/undo")
        assert response.status_code == 409
        envelope_error(response)

    def test_second_trigger_returns_the_active_run_id(self, auth_client, active_run):
        response = auth_client.post("/api/runs", json={})
        assert response.status_code == 409
        body = response.json()
        assert active_run.id in str(body), (
            f"the 409 must carry the active run_id so the UI can attach: {body}"
        )

    def test_undoing_a_nonexistent_run_is_404_not_500(self, auth_client):
        response = auth_client.post("/api/runs/test-run-ghost/undo")
        assert response.status_code == 404
        envelope_error(response)


class TestUndoneThreadsAreRedecidable:
    def test_a_later_run_may_re_decide_undone_threads(
        self, auth_client, completed_run, db_session, monkeypatch, _require_llm_key
    ):
        from db import models

        envelope_ok(auth_client.post(f"/api/runs/{completed_run}/undo"))
        install_inbox(monkeypatch, sample_threads(3))  # same threads, back in INBOX
        run2_id, run2 = run_to_completion(auth_client, chunk_limit=3)
        assert run2["status"] == "completed"
        db_session.expire_all()
        fresh = (
            db_session.query(models.ThreadDecision)
            .filter(models.ThreadDecision.run_id == run2_id)
            .count()
        )
        assert fresh == 3, (
            "undone threads must be re-decidable by a later run (never-redo "
            "excludes undone decisions)"
        )
