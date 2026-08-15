"""Phase 8 gate: a completed run holding `review_failed` rows can be recovered.

spec/capabilities/review-recovery.md, spec/api.md § Phase 8, spec/ui.md screen 24.

A provider outage left a real 2,122-thread run with decisions the never-miss
reviewer never saw. ``apply_decision`` refuses those rows forever — correctly —
so ``POST /api/runs/{id}/apply`` can never clear them, and the only recovery was
throwing the whole run away and re-classifying the entire mailbox. This gate
proves the recovery path works **through** the gate, never around it.

Real: the NVIDIA NIM reviewer (keys from ``.env``), the real never-miss chain, the
real ``apply_decision`` with ``force=False``, the real remainder ledger, the real
route. 120 seeded ``review_failed`` decisions over the 220-thread fixture — large
enough that a sampled retry and a full retry give different counts.

**Deviation, deliberate (same as ``test_drive_to_zero.py``):** the Gmail
*transport* is a recording double. An automated gate may not archive a real
person's mail, and this test's user is synthetic. Everything above the transport
is real.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from tests.integration._threads_fixture import (
    ACCOUNT_ID,
    REPLIED_SENDERS,
    USER_ID,
    build_threads,
    seed_user,
)

pytestmark = pytest.mark.integration

RUN_ID = "run-recovery"
DRY_RUN_ID = "run-recovery-dry"
OTHER_USER = "user-other"
PENDING_TOTAL = 120


class RecordingMutator:
    """Stands in for the Gmail transport only. Records every archive."""

    def __init__(self) -> None:
        self.archived: list[str] = []

    def get_thread_labels(self, thread_id: str) -> list[str]:
        return ["INBOX"]

    def archive_and_label(self, thread_id: str, *, category_label_id: str) -> dict:
        self.archived.append(thread_id)
        return {"id": thread_id}

    def undo_archive_and_label(self, thread_id: str, *, category_label_id: str) -> dict:
        return {"id": thread_id}

    def restore_labels(self, thread_id: str, **kwargs) -> dict:
        return {"id": thread_id}


class Labels:
    def ensure_label(self, name: str) -> dict:
        return {"id": f"Label_{abs(hash(name)) % 9999}", "name": name}


def _session():
    from db.session import create_db_session

    return create_db_session()


@pytest.fixture
def seeded(_isolated_db, _require_llm_key):
    """A completed run over the 220-thread fixture, 120 rows never reviewed."""
    from db.models import Decision, Item, TriageRun, User

    with _session() as session:
        seed_user(session)
        session.add(User(id=OTHER_USER, email="other@example.com"))
        for run_id, dry in ((RUN_ID, False), (DRY_RUN_ID, True)):
            session.add(
                TriageRun(
                    id=run_id,
                    user_id=USER_ID,
                    channel_account_id=ACCOUNT_ID,
                    status="completed",
                    dry_run=dry,
                    counts={},
                    items_total=PENDING_TOTAL,
                    items_decided=PENDING_TOTAL,
                    started_at=datetime.now(timezone.utc) - timedelta(minutes=30),
                    finished_at=datetime.now(timezone.utc),
                )
            )

        threads = build_threads()[:PENDING_TOTAL]
        for thread in threads:
            session.add(
                Item(
                    id=thread["id"],
                    user_id=USER_ID,
                    channel_account_id=ACCOUNT_ID,
                    external_thread_id=thread["external_thread_id"],
                    subject=thread["subject"],
                    from_name=thread["from_name"],
                    from_email=thread["from_email"],
                    from_domain=thread["from_domain"],
                    list_id=thread.get("list_id"),
                    # Headers + a redacted <=200-char snippet only — no body ever.
                    snippet_redacted=str(thread["snippet"])[:200],
                    channel_labels=["INBOX"],
                    internal_date=datetime.now(timezone.utc) - timedelta(days=2),
                )
            )
            for run_id in (RUN_ID, DRY_RUN_ID):
                session.add(
                    Decision(
                        id=f"dec-{run_id}-{thread['id']}",
                        user_id=USER_ID,
                        run_id=run_id,
                        item_id=thread["id"],
                        category_id="cat-newsletters",
                        proposed_action="archive",
                        confidence=0.93,
                        reasoning="Bulk mail with a List-Id and an unsubscribe link.",
                        decided_by="llm",
                        status="proposed",
                        # The gap this phase closes: the reviewer never saw these.
                        review_state="review_failed",
                        autonomy_state="auto_act",
                    )
                )
    return RUN_ID


@pytest.fixture
def gmail(monkeypatch):
    import graph.nodes as nodes

    rec = RecordingMutator()
    monkeypatch.setattr(nodes, "_build_mutator_for_user", lambda *a, **k: (rec, Labels()))
    return rec


@pytest.fixture
def reviewer_calls(monkeypatch):
    """Counts real reviewer batches without replacing them."""
    import graph.nodes_review as nodes_review

    real = nodes_review.review_archive_batch
    calls: list[int] = []

    def counted(decisions, items_by_id, **kwargs):
        calls.append(len(decisions))
        return real(decisions, items_by_id, **kwargs)

    monkeypatch.setattr(nodes_review, "review_archive_batch", counted)
    return calls


@pytest.fixture
def client(seeded):
    from fastapi.testclient import TestClient

    from api import app
    from api.session import require_user_id

    app.dependency_overrides[require_user_id] = lambda: USER_ID
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _rows(run_id: str) -> dict[str, dict]:
    from db.models import Decision

    with _session() as session:
        return {
            row.item_id: {
                "review_state": row.review_state,
                "status": row.status,
                "proposed_action": row.proposed_action,
                "id": row.id,
            }
            for row in session.execute(
                select(Decision).where(Decision.run_id == run_id)
            ).scalars()
        }


def _ledger(run_id: str) -> dict:
    from api.runs import not_reviewed_count
    from graph.remainder import remainder_ledger

    with _session() as session:
        ledger = remainder_ledger(session, run_id=run_id, user_id=USER_ID)
        ledger["not_reviewed"] = not_reviewed_count(
            session, run_id=run_id, user_id=USER_ID
        )
        return ledger


# ------------------------------------------------------- the real recovery run


class TestRealRetry:
    """One real reviewer pass over 120 rows, then the ordinary apply pass.

    The retry is run ONCE per test (each test gets its own isolated DB) and the
    three tests are split by concern, not by assertion — a real 120-thread
    reviewer pass is minutes of real provider time, so they are kept few and each
    one is load-bearing.
    """

    @pytest.fixture(autouse=True)
    def _run(self, seeded, gmail, reviewer_calls):
        from graph.review_retry import retry_review

        self.before = _ledger(RUN_ID)
        self.gmail = gmail
        self.reviewer_calls = reviewer_calls
        self.counts = retry_review(run_id=RUN_ID, user_id=USER_ID)
        self.after = _ledger(RUN_ID)
        self.rows = _rows(RUN_ID)

    def test_the_run_is_recovered_the_ledger_moves_and_every_archive_is_undoable(self):
        from db.models import ActionLog, Decision

        assert self.before["not_reviewed"] == PENDING_TOTAL
        assert self.counts["retried"] == PENDING_TOTAL
        assert sum(self.reviewer_calls) == PENDING_TOTAL, (
            "every pending archive proposal must be put in front of the real reviewer"
        )
        assert self.counts["reviewed"] + self.counts["still_failed"] == PENDING_TOTAL
        # The ledger falls by exactly what the reviewer passed — no more, no less.
        assert self.after["not_reviewed"] == PENDING_TOTAL - self.counts["reviewed"]
        upgraded = sum(1 for r in self.rows.values() if r["review_state"] == "reviewed")
        assert upgraded == self.counts["reviewed"]

        with _session() as session:
            logs = [
                {"operation": log.operation, "undo_token": log.undo_token}
                for log in session.execute(
                    select(ActionLog)
                    .join(Decision, ActionLog.decision_id == Decision.id)
                    .where(Decision.run_id == RUN_ID)
                ).scalars()
            ]
        assert len(logs) == self.counts["applied"] == len(self.gmail.archived)
        assert self.counts["applied"] > 0, (
            "a working reviewer must let at least some bulk mail through, or the "
            "recovery path is useless"
        )
        assert all(log["undo_token"] for log in logs), "every mutation is undoable"
        # Never delete/trash/spam: no such operation exists in this system.
        assert {log["operation"] for log in logs} == {"archive"}

    def test_the_never_miss_guarantees_still_bind_on_a_retry(self):
        from db.models import Item

        with _session() as session:
            items = list(
                session.execute(select(Item).where(Item.user_id == USER_ID)).scalars()
            )
            item_of_thread = {i.external_thread_id: i.id for i in items}
            replied_items = [
                i.id for i in items if (i.from_email or "") in REPLIED_SENDERS
            ]

        # 1. Nothing left the inbox without a reviewer verdict.
        for thread_id in set(self.gmail.archived):
            row = self.rows[item_of_thread[thread_id]]
            assert row["review_state"] == "reviewed", thread_id
            assert row["proposed_action"] == "archive", thread_id

        # 2. A thread the reviewer flipped to keep stays kept.
        for row in self.rows.values():
            if row["proposed_action"] != "archive":
                assert row["status"] != "applied"

        # 3. A sender the user has replied to is held whatever the reviewer said.
        held = [self.rows[i] for i in replied_items if i in self.rows]
        assert held, "the fixture must contain mail from an ever-replied sender"
        for row in held:
            assert row["proposed_action"] == "keep"
            assert row["status"] != "applied"

    def test_a_second_immediate_retry_spends_nothing(self):
        from graph.review_retry import retry_review

        calls_before = len(self.reviewer_calls)
        archived_before = list(self.gmail.archived)
        counts = retry_review(run_id=RUN_ID, user_id=USER_ID)

        remaining = self.after["not_reviewed"]
        if remaining == 0:
            assert counts == {"retried": 0, "reviewed": 0, "still_failed": 0, "applied": 0}
            assert len(self.reviewer_calls) == calls_before, "zero LLM calls"
        else:
            # The reviewer failed on some rows; a retry is legitimately allowed to
            # try those again — but never anything already reviewed.
            assert counts["retried"] == remaining
        assert self.gmail.archived == archived_before, "zero new Gmail mutations"


# ------------------------------------------------------------------ edge cases


def test_dry_run_reviews_the_rows_and_mutates_nothing(seeded, gmail, reviewer_calls):
    from graph.review_retry import retry_review

    counts = retry_review(run_id=DRY_RUN_ID, user_id=USER_ID)

    assert sum(reviewer_calls) == PENDING_TOTAL, "dry-run still reviews for real"
    assert counts["reviewed"] > 0
    assert counts["applied"] == 0
    assert gmail.archived == [], "dry_run is absolute — zero mutations"
    assert _ledger(DRY_RUN_ID)["applied"] == 0


# ---------------------------------------------------------------- the route


def test_a_running_run_is_409_not_retryable(client):
    from db.models import TriageRun

    with _session() as session:
        session.get(TriageRun, RUN_ID).status = "running"
    response = client.post(f"/api/runs/{RUN_ID}/retry-review")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_retryable"


def test_another_users_run_is_404(client):
    from api import app
    from api.session import require_user_id

    app.dependency_overrides[require_user_id] = lambda: OTHER_USER
    response = client.post(f"/api/runs/{RUN_ID}/retry-review")
    assert response.status_code == 404
    assert _ledger(RUN_ID)["not_reviewed"] == PENDING_TOTAL, "nothing was touched"


def test_the_remainder_route_reports_the_unreviewed_remainder(client):
    response = client.get(f"/api/runs/{RUN_ID}/remainder")
    assert response.status_code == 200
    assert response.json()["data"]["not_reviewed"] == PENDING_TOTAL
