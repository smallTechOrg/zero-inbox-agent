"""Review recovery: re-entering the never-miss gate can never become a bypass.

spec/capabilities/review-recovery.md, spec/api.md § Phase 8.

The reviewer is allowed to fail. When it does, the rows stay ``review_failed`` and
``apply_decision`` refuses them forever — that refusal is the gate working. This
module's job is to let the user re-enter the gate, and the tests below exist to
make sure it never turns into a way *around* it:

* a thread reaches the Gmail mutator ONLY after a real reviewer pass;
* ``review_state`` becomes ``reviewed`` only under ``finalise_review`` (the
  reviewer's own writer), never from ``review_retry``;
* ``apply_decision`` is never called with ``force=True``;
* a ``keep``-proposed row, a VIP sender and an ever-replied sender are never
  mutated, however the reviewer votes;
* ``dry_run`` is absolute.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest
from sqlalchemy import event

USER_ID = "u-retry"
OTHER_USER = "u-stranger"
RUN_ID = "run-retry"

#: item_id -> (from_email, proposed_action) for the seeded pending set.
PENDING = {
    "it-news-1": ("news@substack.com", "archive"),
    "it-news-2": ("digest@medium.com", "archive"),
    "it-vip": ("boss@acme.com", "archive"),
    "it-replied": ("colleague@acme.io", "archive"),
    "it-keep": ("mum@family.net", "keep"),
}

#: The subset of :data:`PENDING` a retry can actually move. The never-miss
#: reviewer only audits ``REVIEWABLE_ACTIONS`` (== ``MUTABLE_ACTIONS``), so the
#: ``keep``-proposed row is not loaded by ``_load_pending`` at all — retrying it
#: would report ``still_failed: 1`` forever without ever being able to fix it.
RETRYABLE = {k: v for k, v in PENDING.items() if v[1] in ("archive", "digest")}


class RecordingMutator:
    """Any archive here for a row that was not reviewed is a never-miss regression."""

    def __init__(self) -> None:
        self.archived: list[str] = []
        self.calls: list[tuple] = []

    def get_thread_labels(self, thread_id: str) -> list[str]:
        self.calls.append(("get_thread_labels", thread_id))
        return ["INBOX"]

    def archive_and_label(self, thread_id: str, *, category_label_id: str) -> dict:
        self.calls.append(("archive_and_label", thread_id))
        self.archived.append(thread_id)
        return {"id": thread_id}

    def undo_archive_and_label(self, thread_id: str, *, category_label_id: str) -> dict:
        self.calls.append(("undo_archive_and_label", thread_id))
        return {"id": thread_id}

    def restore_labels(self, thread_id: str, **kwargs) -> dict:
        self.calls.append(("restore_labels", thread_id))
        return {"id": thread_id}


class Labels:
    def ensure_label(self, name: str) -> dict:
        return {"id": "Label_news", "name": name}


def _session():
    from db.session import create_db_session

    return create_db_session()


@pytest.fixture
def seeded(_isolated_db):
    """One completed run: 5 ``review_failed`` rows + 1 already-``reviewed`` row."""
    from db.models import (
        Category,
        ChannelAccount,
        Decision,
        Item,
        SenderProfile,
        TriageRun,
        User,
        VipEntry,
    )

    with _session() as session:
        session.add(User(id=USER_ID, email="retry@example.com"))
        session.add(User(id=OTHER_USER, email="stranger@example.com"))
        session.add(
            ChannelAccount(
                id="acct-retry",
                user_id=USER_ID,
                channel="gmail",
                account_email="retry@example.com",
                refresh_token_enc="x",
            )
        )
        session.add(
            Category(
                id="cat-news",
                user_id=USER_ID,
                key="newsletters",
                name="Newsletters",
                default_action="archive",
                channel_label_name="ZeroInbox/Newsletters",
            )
        )
        session.add(
            TriageRun(
                id=RUN_ID,
                user_id=USER_ID,
                channel_account_id="acct-retry",
                status="completed",
                dry_run=False,
                counts={},
                items_total=6,
                items_decided=6,
            )
        )
        session.add(VipEntry(id="vip-1", user_id=USER_ID, kind="email", value="boss@acme.com"))
        session.add(
            SenderProfile(
                id="sp-1",
                user_id=USER_ID,
                sender_email="colleague@acme.io",
                ever_replied=True,
                replied_count=3,
            )
        )
        rows = dict(PENDING)
        for n, (item_id, (email, action)) in enumerate(rows.items()):
            session.add(
                Item(
                    id=item_id,
                    user_id=USER_ID,
                    channel_account_id="acct-retry",
                    external_thread_id=f"thread-{item_id}",
                    subject=f"Subject {n}",
                    from_email=email,
                    from_domain=email.split("@")[1],
                    snippet_redacted="redacted snippet",
                    channel_labels=["INBOX"],
                    internal_date=datetime.now(timezone.utc),
                )
            )
            session.add(
                Decision(
                    id=f"dec-{item_id}",
                    user_id=USER_ID,
                    run_id=RUN_ID,
                    item_id=item_id,
                    category_id="cat-news",
                    proposed_action=action,
                    confidence=0.96,
                    reasoning="Bulk newsletter.",
                    decided_by="llm",
                    status="proposed",
                    review_state="review_failed",
                    autonomy_state="auto_act",
                )
            )
        # A row that already passed the reviewer on the original run and was
        # applied. The retry must not touch it or re-archive it.
        session.add(
            Item(
                id="it-done",
                user_id=USER_ID,
                channel_account_id="acct-retry",
                external_thread_id="thread-it-done",
                subject="Already archived",
                from_email="old@substack.com",
                from_domain="substack.com",
                channel_labels=[],
                internal_date=datetime.now(timezone.utc),
            )
        )
        session.add(
            Decision(
                id="dec-it-done",
                user_id=USER_ID,
                run_id=RUN_ID,
                item_id="it-done",
                category_id="cat-news",
                proposed_action="archive",
                confidence=0.99,
                reasoning="Already applied.",
                decided_by="llm",
                status="applied",
                review_state="reviewed",
                autonomy_state="auto_act",
            )
        )
    return RUN_ID


@pytest.fixture
def mutator(monkeypatch):
    """Every Gmail mutation goes through this recorder — no real client is built."""
    import graph.nodes as nodes

    rec = RecordingMutator()
    monkeypatch.setattr(
        nodes, "_build_mutator_for_user", lambda *a, **k: (rec, Labels())
    )
    return rec


@pytest.fixture
def apply_spy(monkeypatch):
    """Records every ``apply_decision`` call: its ``force`` and the row's review_state."""
    from tools import actions as actions_module

    real = actions_module.apply_decision
    calls: list[dict] = []

    def spy(session, user_id, decision_id, **kwargs):
        from db.models import Decision

        row = session.get(Decision, decision_id)
        calls.append(
            {
                "decision_id": decision_id,
                "force": kwargs.get("force", False),
                "review_state": getattr(row, "review_state", None),
                "proposed_action": getattr(row, "proposed_action", None),
            }
        )
        return real(session, user_id, decision_id, **kwargs)

    monkeypatch.setattr(actions_module, "apply_decision", spy)
    return calls


@pytest.fixture
def review_state_writes():
    """Every ORM write to ``Decision.review_state``, with the file that made it."""
    from db.models import Decision

    writes: list[tuple[str, str]] = []

    def _on_set(target, value, oldvalue, initiator):
        frame = inspect.currentframe()
        caller = "<unknown>"
        while frame is not None:
            name = frame.f_code.co_filename
            if "sqlalchemy" not in name and "test_review_retry" not in name and name != __file__:
                caller = name
                break
            frame = frame.f_back
        writes.append((str(value), caller))
        return value

    event.listen(Decision.review_state, "set", _on_set, retval=True)
    yield writes
    event.remove(Decision.review_state, "set", _on_set)


def _stub_reviewer(monkeypatch, *, failed: bool, flip_ids: set[str] | None = None):
    """Stub the reviewer LLM batch call — the node itself is the real one."""
    import graph.nodes_review as nodes_review

    seen: list[list[dict]] = []

    def fake(decisions, items_by_id, **kwargs):
        seen.append(list(decisions))
        if failed:
            return {}, [], True
        flips = {
            d["item_id"]: "Looks like a real person writing to you."
            for d in decisions
            if d["item_id"] in (flip_ids or set())
        }
        return flips, [], False

    monkeypatch.setattr(nodes_review, "review_archive_batch", fake)
    return seen


def _states(run_id: str = RUN_ID) -> dict[str, str]:
    from sqlalchemy import select

    from db.models import Decision

    with _session() as session:
        return {
            row.item_id: row.review_state
            for row in session.execute(
                select(Decision).where(Decision.run_id == run_id)
            ).scalars()
        }


def _actions(run_id: str = RUN_ID) -> list:
    from sqlalchemy import select

    from db.models import ActionLog, Decision

    with _session() as session:
        return [
            {"operation": row.operation, "undo_token": row.undo_token}
            for row in session.execute(
                select(ActionLog)
                .join(Decision, ActionLog.decision_id == Decision.id)
                .where(Decision.run_id == run_id)
            ).scalars()
        ]


# --------------------------------------------------------------- happy path


def test_retry_reviews_and_applies_what_the_reviewer_passes(
    seeded, mutator, apply_spy, monkeypatch
):
    from graph.review_retry import retry_review

    _stub_reviewer(monkeypatch, failed=False)

    counts = retry_review(run_id=RUN_ID, user_id=USER_ID)

    states = _states()
    assert states["it-news-1"] == "reviewed"
    assert states["it-news-2"] == "reviewed"
    # The keep-proposed row is not retryable (the reviewer never audits a keep),
    # so it is never loaded and never counted — the retry converges to zero.
    assert counts["retried"] == len(RETRYABLE)
    assert counts["reviewed"] == len(RETRYABLE)
    assert counts["still_failed"] == 0
    assert _states()["it-keep"] == "review_failed", (
        "an un-retryable keep row is left exactly where it was, not swept to reviewed"
    )
    # Only the two plain newsletters may leave the inbox: VIP, reply-history and
    # the keep-proposed row are all held.
    assert sorted(mutator.archived) == ["thread-it-news-1", "thread-it-news-2"]
    assert counts["applied"] == 2
    logs = _actions()
    assert len(logs) == 2
    assert all(log["undo_token"] for log in logs), "every mutation carries an undo token"
    # Never delete/trash/spam — no such operation exists anywhere in this system.
    assert all(log["operation"] == "archive" for log in logs)


def test_no_thread_reaches_the_mutator_without_a_real_reviewer_pass(
    seeded, mutator, apply_spy, monkeypatch
):
    """The load-bearing test: retry must never be a bypass of the review gate."""
    from graph.review_retry import retry_review

    _stub_reviewer(monkeypatch, failed=False)
    retry_review(run_id=RUN_ID, user_id=USER_ID)

    assert apply_spy, "the apply pass must have run"
    for call in apply_spy:
        assert call["review_state"] == "reviewed", (
            "apply_decision was reached with review_state="
            f"{call['review_state']!r} — an unreviewed thread must never get this far"
        )
        assert call["force"] is False, "force=True is never a side door around a keep"


def test_review_state_reviewed_is_only_ever_written_by_the_reviewers_writer(
    seeded, mutator, apply_spy, monkeypatch, review_state_writes
):
    from graph.review_retry import retry_review

    _stub_reviewer(monkeypatch, failed=False)
    retry_review(run_id=RUN_ID, user_id=USER_ID)

    upgrades = [(value, caller) for value, caller in review_state_writes if value == "reviewed"]
    assert upgrades, "the reviewer must have upgraded something"
    for _value, caller in upgrades:
        assert caller.endswith("persistence.py"), (
            f"review_state='reviewed' was written from {caller} — only finalise_review, "
            "called from inside the never-miss chain, may ever write it"
        )


# --------------------------------------------------------------- error path


def test_a_failing_reviewer_leaves_every_row_blocked_and_mutates_nothing(
    seeded, mutator, apply_spy, monkeypatch
):
    from graph.review_retry import retry_review

    _stub_reviewer(monkeypatch, failed=True)

    counts = retry_review(run_id=RUN_ID, user_id=USER_ID)

    states = _states()
    for item_id in ("it-news-1", "it-news-2", "it-vip", "it-replied"):
        assert states[item_id] == "review_failed", item_id
    # Nothing is upgraded: the keep-proposed row is not retryable and so is not in
    # the retry's scope at all, and every archive row the reviewer could not process
    # stays blocked. No row is ever swept to `reviewed` on the reviewer's behalf.
    assert counts["reviewed"] == 0
    assert counts["still_failed"] == len(RETRYABLE)
    assert counts["applied"] == 0
    assert mutator.calls == [], "a failed reviewer must produce zero Gmail calls"
    assert _actions() == []


def test_a_reviewer_that_never_runs_leaves_every_thread_short_of_the_mutator(
    seeded, mutator, apply_spy, monkeypatch
):
    """The gate must hold when the reviewer does not even get to vote.

    This is the shape the bug would take: re-entry puts the rows back at the
    gate, the reviewer pass then dies, and nothing may have moved. If the
    re-entry step ever wrote ``reviewed`` instead of ``provisional``, these
    threads would be archived without a single reviewer verdict.
    """
    import graph.nodes_review as nodes_review
    from graph.review_retry import retry_review

    def explode(_state):
        raise RuntimeError("provider outage during the retry")

    monkeypatch.setattr(nodes_review, "second_pass_reviewer", explode)

    counts = retry_review(run_id=RUN_ID, user_id=USER_ID)

    assert counts["applied"] == 0
    assert mutator.calls == [], "no thread may reach the mutator without a reviewer pass"
    assert apply_spy == []
    assert set(_states()[i] for i in RETRYABLE) == {"provisional"}, (
        "the rows stay at the gate — provisional is refused by apply_decision "
        "exactly as hard as review_failed"
    )
    assert _states()["it-keep"] == "review_failed", (
        "the un-retryable keep row never entered the gate and is left untouched"
    )


def test_a_keep_proposed_row_is_never_mutated_even_if_the_reviewer_passes(
    seeded, mutator, apply_spy, monkeypatch
):
    from graph.review_retry import retry_review

    _stub_reviewer(monkeypatch, failed=False)
    retry_review(run_id=RUN_ID, user_id=USER_ID)

    assert "thread-it-keep" not in mutator.archived
    assert all(call["proposed_action"] != "keep" for call in apply_spy)


def test_vip_and_reply_history_senders_are_still_held_on_a_retry(
    seeded, mutator, apply_spy, monkeypatch
):
    from sqlalchemy import select

    from db.models import Decision
    from graph.review_retry import retry_review

    _stub_reviewer(monkeypatch, failed=False)
    retry_review(run_id=RUN_ID, user_id=USER_ID)

    assert "thread-it-vip" not in mutator.archived
    assert "thread-it-replied" not in mutator.archived
    with _session() as session:
        actions = {
            row.item_id: row.proposed_action
            for row in session.execute(
                select(Decision).where(Decision.run_id == RUN_ID)
            ).scalars()
        }
    assert actions["it-vip"] == "keep"
    assert actions["it-replied"] == "keep"


def test_the_only_review_state_write_retry_makes_itself_is_the_downgrade(seeded):
    """``_reenter_review_gate`` can only turn ``review_failed`` into ``provisional``.

    Both states are refused by ``apply_decision``, so the re-entry step can never
    unblock a mutation — and a row that already passed the reviewer is untouched.
    """
    from graph.review_retry import _reenter_review_gate

    with _session() as session:
        changed = _reenter_review_gate(
            session,
            run_id=RUN_ID,
            user_id=USER_ID,
            item_ids=[*PENDING, "it-done"],
        )
    assert changed == len(PENDING)
    states = _states()
    assert set(states[i] for i in PENDING) == {"provisional"}
    assert states["it-done"] == "reviewed", "an already-reviewed row is never rewritten"


# ---------------------------------------------------------------- edge cases


def test_a_second_retry_with_nothing_left_is_a_no_op(seeded, mutator, apply_spy, monkeypatch):
    from graph.review_retry import retry_review

    _stub_reviewer(monkeypatch, failed=False)
    retry_review(run_id=RUN_ID, user_id=USER_ID)
    archived_after_first = list(mutator.archived)

    seen = _stub_reviewer(monkeypatch, failed=False)
    counts = retry_review(run_id=RUN_ID, user_id=USER_ID)

    assert counts == {"retried": 0, "reviewed": 0, "still_failed": 0, "applied": 0}
    assert seen == [], "a no-op retry spends zero LLM calls"
    assert mutator.archived == archived_after_first, "and performs zero Gmail calls"


def test_dry_run_reviews_but_mutates_nothing(seeded, mutator, apply_spy, monkeypatch):
    from db.models import TriageRun
    from graph.review_retry import retry_review

    with _session() as session:
        session.get(TriageRun, RUN_ID).dry_run = True

    _stub_reviewer(monkeypatch, failed=False)
    counts = retry_review(run_id=RUN_ID, user_id=USER_ID)

    assert counts["reviewed"] == len(RETRYABLE), "dry-run still reviews"
    assert counts["applied"] == 0
    assert mutator.calls == [], "dry_run is absolute — zero mutations"
    assert apply_spy == []


def test_another_users_run_is_invisible_to_the_worker(seeded, mutator, apply_spy, monkeypatch):
    from graph.review_retry import retry_review

    seen = _stub_reviewer(monkeypatch, failed=False)
    counts = retry_review(run_id=RUN_ID, user_id=OTHER_USER)

    assert counts == {"retried": 0, "reviewed": 0, "still_failed": 0, "applied": 0}
    assert seen == []
    assert mutator.calls == []
    assert set(_states()[i] for i in PENDING) == {"review_failed"}


# ------------------------------------------------------------------ the route


class TestRetryReviewRoute:
    """``POST /api/runs/{run_id}/retry-review`` — spec/api.md § Phase 8."""

    @pytest.fixture
    def client(self, seeded):
        from fastapi.testclient import TestClient

        from api import app
        from api.session import require_user_id

        app.dependency_overrides[require_user_id] = lambda: USER_ID
        with TestClient(app) as client:
            yield client
        app.dependency_overrides.clear()

    def test_a_running_run_is_not_retryable(self, client):
        from db.models import TriageRun

        with _session() as session:
            session.get(TriageRun, RUN_ID).status = "running"
        response = client.post(f"/api/runs/{RUN_ID}/retry-review")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "not_retryable"

    def test_a_run_with_nothing_to_review_is_not_retryable(self, client):
        from sqlalchemy import update

        from db.models import Decision

        with _session() as session:
            session.execute(
                update(Decision)
                .where(Decision.run_id == RUN_ID)
                .values(review_state="reviewed")
            )
        response = client.post(f"/api/runs/{RUN_ID}/retry-review")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "not_retryable"

    def test_another_users_run_is_404(self, client):
        from api import app
        from api.session import require_user_id

        app.dependency_overrides[require_user_id] = lambda: OTHER_USER
        response = client.post(f"/api/runs/{RUN_ID}/retry-review")
        assert response.status_code == 404

    def test_the_remainder_ledger_reports_the_unreviewed_count(self, client):
        response = client.get(f"/api/runs/{RUN_ID}/remainder")
        assert response.status_code == 200
        assert response.json()["data"]["not_reviewed"] == len(PENDING)
