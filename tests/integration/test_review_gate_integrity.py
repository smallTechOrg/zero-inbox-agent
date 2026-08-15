"""Phase 9, item zero — the never-miss gate must never pass vacuously.

`spec/roadmap.md` § Phase 9 "Item zero. Slice 1"; `spec/capabilities/never-miss-safeguards.md`;
`spec/capabilities/review-recovery.md`.

The live defect this file exists to keep dead:

* `tools.actions.apply_decision` mutates Gmail for `proposed_action` in
  `{"archive", "digest"}` — both reach
  `archive_and_label(remove_label_ids=[INBOX])`.
* `graph.nodes_review.second_pass_reviewer` audited only `archive`.
* `graph.persistence.finalise_review` then upgraded `review_state` across the
  **whole run**, so 171 `digest` rows read `reviewed` having never been audited.
  44 of them were applied. `NotReviewedError` — the one thing standing between an
  unaudited decision and the user's mailbox — never fired.

The run below replays that exact shape (171 digest + 9 archive + 9 keep) through
the real node chain (`second_pass_reviewer` -> `apply_never_miss_floor` ->
`finalise_review`) against the `_isolated_db` fixture, and then drives
`tools.actions.apply_decision` with a **spy mutator**: any Gmail call at all is a
regression.

No LLM is called here by design — the reviewer is the component under test, and
what is being asserted is what happens when it fails or covers only part of the
run. The real-provider reviewer path is covered by
`tests/integration/test_reviewer_calibration.py`.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

USER_ID = "test-user-gate"
ACCOUNT_ID = "acct-gate"
RUN_ID = "run-gate-integrity"
CATEGORY_ID = "cat-gate"

#: The measured live shape.
DIGEST_COUNT = 171
ARCHIVE_COUNT = 9
KEEP_COUNT = 9


def _kind(n: int) -> str:
    if n < DIGEST_COUNT:
        return "digest"
    if n < DIGEST_COUNT + ARCHIVE_COUNT:
        return "archive"
    return "keep"


TOTAL = DIGEST_COUNT + ARCHIVE_COUNT + KEEP_COUNT


class SpyMutator:
    """Records every Gmail call. In this file it must record none."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def get_thread_labels(self, thread_id: str) -> list[str]:
        self.calls.append(("get_thread_labels", thread_id))
        return ["INBOX"]

    def archive_and_label(self, thread_id: str, *, category_label_id: str) -> dict:
        self.calls.append(("archive_and_label", thread_id))
        return {"id": thread_id}

    def undo_archive_and_label(self, thread_id: str, *, category_label_id: str) -> dict:
        self.calls.append(("undo_archive_and_label", thread_id))
        return {"id": thread_id}

    def restore_labels(self, thread_id: str, **kwargs) -> dict:
        self.calls.append(("restore_labels", thread_id))
        return {"id": thread_id}


class SpyLabels:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def ensure_label(self, name: str) -> dict:
        self.calls.append(name)
        return {"id": "Label_gate", "name": name}


@pytest.fixture
def seeded(_isolated_db):
    """One in-flight run: every decision `provisional`, `approved`, un-applied."""
    from db.models import (
        Category,
        ChannelAccount,
        Decision,
        Item,
        TriageRun,
        User,
        UserSettings,
    )
    from db.session import create_db_session

    with create_db_session() as session:
        session.add(User(id=USER_ID, email="gate@example.com", display_name="G"))
        session.add(UserSettings(user_id=USER_ID))
        session.add(
            ChannelAccount(
                id=ACCOUNT_ID,
                user_id=USER_ID,
                channel="gmail",
                account_email="gate@example.com",
                refresh_token_enc="ENC",
                scopes=[],
                status="connected",
            )
        )
        session.add(
            Category(
                id=CATEGORY_ID,
                user_id=USER_ID,
                key="newsletters",
                name="Newsletters",
                description="",
                channel_label_name="ZeroInbox/Newsletters",
                default_action="archive",
                is_default=True,
                sort_order=1,
            )
        )
        session.add(
            TriageRun(
                id=RUN_ID,
                user_id=USER_ID,
                channel_account_id=ACCOUNT_ID,
                status="running",
                dry_run=False,
                items_total=TOTAL,
                items_decided=TOTAL,
            )
        )
        for n in range(TOTAL):
            session.add(
                Item(
                    id=f"it-{n}",
                    user_id=USER_ID,
                    channel_account_id=ACCOUNT_ID,
                    external_thread_id=f"thread-{n}",
                    external_message_ids=[f"m{n}"],
                    subject=f"Subject {n}",
                    from_name="Sender",
                    from_email=f"s{n}@example.com",
                    from_domain="example.com",
                    message_count=1,
                    snippet_redacted="redacted snippet",
                    internal_date=datetime.now(timezone.utc),
                    is_unread=False,
                )
            )
            session.add(
                Decision(
                    id=f"dec-{n}",
                    user_id=USER_ID,
                    item_id=f"it-{n}",
                    run_id=RUN_ID,
                    category_id=CATEGORY_ID,
                    proposed_action=_kind(n),
                    confidence=0.92,
                    reasoning="bulk sender",
                    decided_by="llm",
                    time_sensitive=False,
                    status="approved",
                    review_state="provisional",
                )
            )
        session.commit()
    return TOTAL


def _state() -> dict:
    return {
        "run_id": RUN_ID,
        "user_id": USER_ID,
        "channel_account_id": ACCOUNT_ID,
        "settings": {},
        "sender_stats": {},
        "vip": {},
        "items": [
            {
                "id": f"it-{n}",
                "from_name": "Sender",
                "from_email": f"s{n}@example.com",
                "from_domain": "example.com",
                "subject": f"Subject {n}",
                "snippet_redacted": "redacted snippet",
            }
            for n in range(TOTAL)
        ],
        "decisions": [
            {
                "item_id": f"it-{n}",
                "category": "newsletters",
                "proposed_action": _kind(n),
                "confidence": 0.92,
                "reasoning": "bulk sender",
                "decided_by": "llm",
                # Already approved: the ONLY thing left between these rows and
                # the mutator is the never-miss review gate.
                "status": "approved",
            }
            for n in range(TOTAL)
        ],
    }


def _run_never_miss_chain(state: dict) -> dict:
    """The real chain: reviewer -> floor (which durably finalises the verdict)."""
    from graph import nodes_review

    out = nodes_review.second_pass_reviewer(state)
    state["decisions"] = out["decisions"]
    state["review_failed_item_ids"] = out.get("review_failed_item_ids") or []
    # `audited_item_ids` is deliberately NOT copied into state: it is not a declared
    # TriageState channel, so LangGraph drops it between nodes. Reproducing that here
    # means these tests exercise the transport that actually carries the audited set
    # in a real run — cut it and they go red, instead of passing on a hand-off the
    # graph never performs.
    nodes_review.apply_never_miss_floor(state)
    return out


def _review_states() -> dict[str, str]:
    from db.models import Decision
    from db.session import create_db_session
    from sqlalchemy import select

    with create_db_session() as session:
        return {
            r.item_id: r.review_state
            for r in session.execute(
                select(Decision).where(Decision.run_id == RUN_ID)
            ).scalars()
        }


def _apply(decision_id: str, mutator, labels, *, force: bool = False):
    from db.session import create_db_session
    from tools.actions import apply_decision

    with create_db_session() as session:
        return apply_decision(
            session,
            USER_ID,
            decision_id,
            mutator=mutator,
            label_lookup=labels,
            dry_run=False,
            force=force,
        )


# ==========================================================================
# 1. Reviewer fails outright: nothing is reviewed, nothing is mutable
# ==========================================================================


@pytest.fixture
def reviewer_always_fails(monkeypatch):
    from graph import nodes_review

    calls = {"n": 0}

    def _fail(decisions, items_by_id, **kwargs):
        calls["n"] += 1
        return {}, [], True

    monkeypatch.setattr(nodes_review, "review_archive_batch", _fail)
    return calls


def test_failed_reviewer_leaves_every_mutable_row_unappliable(
    seeded, reviewer_always_fails
):
    out = _run_never_miss_chain(_state())

    assert reviewer_always_fails["n"] > 0, "the reviewer stub was never invoked"
    assert out["audited_item_ids"] == []

    states = _review_states()
    mutable = [f"it-{n}" for n in range(DIGEST_COUNT + ARCHIVE_COUNT)]
    keeps = [f"it-{n}" for n in range(DIGEST_COUNT + ARCHIVE_COUNT, TOTAL)]

    assert {states[i] for i in mutable} == {"review_failed"}, (
        "every archive AND digest row must be review_failed when the reviewer "
        "could not audit it"
    )
    assert {states[i] for i in keeps} == {"provisional"}
    assert "reviewed" not in set(states.values())


def test_no_digest_row_reaches_the_mutator(seeded, reviewer_always_fails):
    from tools.actions import NotReviewedError

    _run_never_miss_chain(_state())

    mutator = SpyMutator()
    labels = SpyLabels()
    for n in (0, 1, DIGEST_COUNT - 1):  # digest rows
        with pytest.raises(NotReviewedError):
            _apply(f"dec-{n}", mutator, labels)

    assert mutator.calls == [], (
        "NotReviewedError must fire BEFORE the mutator — the spy recorded a "
        f"Gmail call: {mutator.calls}"
    )
    assert labels.calls == []


def test_force_true_does_not_bypass_the_gate(seeded, reviewer_always_fails):
    from tools.actions import NotReviewedError

    _run_never_miss_chain(_state())

    mutator = SpyMutator()
    labels = SpyLabels()
    for n in (0, DIGEST_COUNT, TOTAL - 1):  # digest, archive, keep
        with pytest.raises(NotReviewedError):
            _apply(f"dec-{n}", mutator, labels, force=True)

    assert mutator.calls == [], "force=True must never be a way around the gate"


def test_zero_mutations_across_the_whole_run(seeded, reviewer_always_fails):
    from db.models import ActionLog
    from db.session import create_db_session
    from sqlalchemy import select
    from tools.actions import ActionsError

    _run_never_miss_chain(_state())

    mutator = SpyMutator()
    labels = SpyLabels()
    raised = 0
    for n in range(TOTAL):
        try:
            _apply(f"dec-{n}", mutator, labels)
        except ActionsError:
            raised += 1

    assert raised == TOTAL, "every single decision must have been refused"
    assert mutator.calls == []

    with create_db_session() as session:
        logs = list(
            session.execute(
                select(ActionLog).where(ActionLog.user_id == USER_ID)
            ).scalars()
        )
    assert logs == [], "an unreviewed run wrote an ActionLog row"


# ==========================================================================
# 2. Reviewer covers a strict subset: only that subset becomes reviewed
# ==========================================================================


@pytest.fixture
def reviewer_audits_first_batch_only(monkeypatch):
    """Succeeds on the first batch, fails on every later one."""
    from graph import nodes_review

    seen: list[list[str]] = []

    def _partial(decisions, items_by_id, **kwargs):
        ids = [d["item_id"] for d in decisions]
        seen.append(ids)
        if len(seen) == 1:
            return {}, [], False
        return {}, [], True

    monkeypatch.setattr(nodes_review, "review_archive_batch", _partial)
    return seen


def test_only_the_audited_subset_is_reviewed(seeded, reviewer_audits_first_batch_only):
    out = _run_never_miss_chain(_state())

    audited = set(out["audited_item_ids"])
    assert audited, "the first batch should have been audited"
    assert audited == set(reviewer_audits_first_batch_only[0])
    assert len(audited) < DIGEST_COUNT + ARCHIVE_COUNT, "expected a STRICT subset"

    states = _review_states()
    reviewed = {i for i, s in states.items() if s == "reviewed"}
    assert reviewed == audited, (
        "exactly the audited rows — and nothing else — may read 'reviewed'"
    )

    # Everything else is either review_failed (a batch that broke) or
    # provisional (a keep, never in scope). Neither is appliable.
    for item_id, state in states.items():
        if item_id not in audited:
            assert state in {"provisional", "review_failed"}


def test_unaudited_digest_rows_are_still_refused(
    seeded, reviewer_audits_first_batch_only
):
    from tools.actions import NotReviewedError

    out = _run_never_miss_chain(_state())
    audited = set(out["audited_item_ids"])
    unaudited = [f"it-{n}" for n in range(DIGEST_COUNT) if f"it-{n}" not in audited]
    assert unaudited

    mutator = SpyMutator()
    labels = SpyLabels()
    for item_id in unaudited[:20]:
        n = int(item_id.split("-")[1])
        with pytest.raises(NotReviewedError):
            _apply(f"dec-{n}", mutator, labels)
    assert mutator.calls == []


def test_an_audited_row_is_appliable_so_the_gate_is_not_blanket_deny(
    seeded, reviewer_audits_first_batch_only
):
    """The counter-test: without this, refusing *everything* would pass 1 and 2."""
    out = _run_never_miss_chain(_state())
    audited = sorted(out["audited_item_ids"], key=lambda i: int(i.split("-")[1]))
    n = int(audited[0].split("-")[1])

    from db.models import ActionLog
    from db.session import create_db_session
    from sqlalchemy import select

    mutator = SpyMutator()
    labels = SpyLabels()
    _apply(f"dec-{n}", mutator, labels)

    assert ("archive_and_label", f"thread-{n}") in mutator.calls

    with create_db_session() as session:
        rows = [
            (r.operation, bool(r.undo_token))
            for r in session.execute(
                select(ActionLog).where(ActionLog.decision_id == f"dec-{n}")
            ).scalars()
        ]
    assert rows == [("archive", True)], "every mutation carries an undo token"


# ==========================================================================
# 3. Structural: no path can widen the scope
# ==========================================================================


def test_run_wide_upgrade_is_not_expressible(seeded, reviewer_always_fails):
    from db.session import create_db_session
    from graph.persistence import ReviewScopeError, upgrade_review_state

    _run_never_miss_chain(_state())

    with create_db_session() as session:
        with pytest.raises(ReviewScopeError):
            upgrade_review_state(session, run_id=RUN_ID, state="reviewed")
        session.rollback()

    assert "reviewed" not in set(_review_states().values())


def test_mutator_has_no_destructive_method():
    """Trust invariant: never delete, trash or spam-report. Asserted structurally."""
    from channels.gmail import mutations

    source = "".join(
        getattr(mutations, name).__doc__ or "" for name in dir(mutations)
    )
    forbidden = ("TRASH", "SPAM", "messages().delete", "threads().delete")
    text = __import__("inspect").getsource(mutations)
    for token in forbidden:
        assert token not in text, (
            f"{token!r} appears in the Gmail mutation layer — mail is never "
            "destroyed"
        )
    assert source is not None
