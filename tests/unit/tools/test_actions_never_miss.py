"""Phase 9, slice 3 — the two new mutation entry points, and the gate they share.

`spec/capabilities/gmail-actions-and-undo.md`, `spec/roadmap.md` § Phase 9 slice 3.

The claim under test is not "these functions work". It is that **no second
mutation path exists**: `archive_to_never_miss_label` and `relabel_decision` pass
through the *same* `dry_run` / `NotReviewedError` / undo-token machinery as
`apply_decision`, and neither one can be talked into an unlabelled archive, a
mutation without an undo token, or a mutation on an unreviewed row.

Every refusal here is asserted to cost **zero mutator calls** — a spy proves the
refusal happened before Gmail was touched, not after.
"""

from __future__ import annotations

import pytest

from channels.base import DryRunViolation
from tools.actions import (
    MUTABLE_ACTIONS,
    NeedsYourCallError,
    NoNeverMissLabelError,
    NotApprovedError,
    NotReviewedError,
    apply_decision,
    archive_to_never_miss_label,
    relabel_decision,
    undo_action,
)

USER_ID = "test-actions-never-miss"


class SpyMutator:
    """Records every call. The point of the spy is what it does NOT record."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.labels: dict[str, list[str]] = {}

    def get_thread_labels(self, thread_id: str) -> list[str]:
        return list(self.labels.get(thread_id, ["INBOX"]))

    def archive_and_label(self, thread_id: str, *, category_label_id: str) -> dict:
        self.calls.append(("archive_and_label", thread_id, category_label_id))
        return {"id": thread_id}

    def restore_labels(self, thread_id, *, add_label_ids, remove_label_ids) -> dict:
        self.calls.append(("restore_labels", thread_id, tuple(add_label_ids), tuple(remove_label_ids)))
        return {"id": thread_id}

    def undo_archive_and_label(self, thread_id: str, *, category_label_id: str) -> dict:
        self.calls.append(("undo_archive_and_label", thread_id, category_label_id))
        return {"id": thread_id}


class Labels:
    def ensure_label(self, name: str) -> dict:
        return {"id": f"Label_{name.replace('/', '_')}", "name": name}


@pytest.fixture
def world(_isolated_db):
    """One user, four categories, one thread, one reviewed archive decision."""
    from datetime import datetime, timezone

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
        session.add(User(id=USER_ID, email="nm@example.com", display_name="N"))
        session.add(UserSettings(user_id=USER_ID))
        session.add(
            ChannelAccount(
                id="acct-nm", user_id=USER_ID, channel="gmail",
                account_email="nm@example.com", refresh_token_enc="E",
                scopes=[], status="connected",
            )
        )
        for key, name, action in (
            ("notifications", "Notifications", "archive"),
            ("people", "People", "keep"),
            ("urgent", "Urgent", "keep"),
            ("important", "Important", "keep"),
        ):
            session.add(
                Category(
                    id=f"cat-{key}", user_id=USER_ID, key=key, name=name, description="",
                    channel_label_name=f"ZeroInbox/{name}", default_action=action,
                    is_default=True, sort_order=0,
                )
            )
        session.add(
            TriageRun(
                id="run-nm", user_id=USER_ID, channel_account_id="acct-nm",
                status="completed", dry_run=False, items_total=1, items_decided=1,
            )
        )
        session.add(
            Item(
                id="it-nm", user_id=USER_ID, channel_account_id="acct-nm",
                external_thread_id="thread-nm", external_message_ids=["m1"],
                subject="Security alert", from_name="Apple",
                from_email="no_reply@email.apple.com", from_domain="email.apple.com",
                message_count=1, snippet_redacted="x",
                internal_date=datetime.now(timezone.utc), is_unread=True,
                channel_labels=["INBOX"],
            )
        )
        session.add(
            Decision(
                id="dec-nm", user_id=USER_ID, item_id="it-nm", run_id="run-nm",
                category_id="cat-urgent", proposed_action="archive", confidence=0.88,
                reasoning="time-sensitive", decided_by="llm", time_sensitive=True,
                status="approved", review_state="reviewed",
                autonomy_state="held_by_never_miss",
            )
        )
        session.commit()
    return create_db_session


def _decision(session, decision_id="dec-nm"):
    from db.models import Decision

    return session.get(Decision, decision_id)


# --------------------------------------------------------------------------
# The contract slice 1 asserts against
# --------------------------------------------------------------------------


def test_mutable_actions_is_exactly_archive_and_digest():
    assert MUTABLE_ACTIONS == frozenset({"archive", "digest"})
    assert "keep" not in MUTABLE_ACTIONS


# --------------------------------------------------------------------------
# archive_to_never_miss_label
# --------------------------------------------------------------------------


def test_a_never_miss_archive_lands_labelled_undoable_and_out_of_the_inbox(world):
    mutator, labels = SpyMutator(), Labels()
    with world() as session:
        log = archive_to_never_miss_label(
            session, USER_ID, "dec-nm", mutator=mutator, label_lookup=labels,
            dry_run=False,
        )
        session.commit()

        assert mutator.calls == [("archive_and_label", "thread-nm", "Label_ZeroInbox_Urgent")]
        assert log.operation == "archive"
        assert log.undo_token, "every mutation must stay undoable — no exceptions"
        assert log.undo_token["original_label_ids"] == ["INBOX"]

        decision = _decision(session)
        assert decision.status == "applied"
        from db.models import Item

        item = session.get(Item, "it-nm")
        assert "INBOX" not in (item.channel_labels or [])
        assert "Label_ZeroInbox_Urgent" in (item.channel_labels or [])


def test_a_never_miss_archive_without_a_resolvable_label_raises_and_calls_gmail_zero_times(world):
    """THE guarantee, not an edge case. The refusal precedes the mutator."""
    mutator, labels = SpyMutator(), Labels()
    with world() as session:
        _decision(session).category_id = "cat-notifications"
        session.flush()

        with pytest.raises(NoNeverMissLabelError):
            archive_to_never_miss_label(
                session, USER_ID, "dec-nm", mutator=mutator, label_lookup=labels,
                dry_run=False,
            )
        assert mutator.calls == [], (
            "a never-miss archive with no never-miss label must never reach Gmail"
        )
        assert _decision(session).status != "applied"


def test_a_never_miss_archive_with_no_category_at_all_raises(world):
    mutator, labels = SpyMutator(), Labels()
    with world() as session:
        _decision(session).category_id = None
        session.flush()
        with pytest.raises(NoNeverMissLabelError):
            archive_to_never_miss_label(
                session, USER_ID, "dec-nm", mutator=mutator, label_lookup=labels,
                dry_run=False,
            )
        assert mutator.calls == []


def test_dry_run_blocks_the_never_miss_archive_absolutely(world):
    mutator, labels = SpyMutator(), Labels()
    with world() as session:
        with pytest.raises(DryRunViolation):
            archive_to_never_miss_label(
                session, USER_ID, "dec-nm", mutator=mutator, label_lookup=labels,
                dry_run=True,
            )
        assert mutator.calls == []
        assert _decision(session).status == "approved"


def test_an_unreviewed_never_miss_archive_raises_before_the_mutator(world):
    mutator, labels = SpyMutator(), Labels()
    with world() as session:
        _decision(session).review_state = "provisional"
        session.flush()
        with pytest.raises(NotReviewedError):
            archive_to_never_miss_label(
                session, USER_ID, "dec-nm", mutator=mutator, label_lookup=labels,
                dry_run=False,
            )
        assert mutator.calls == []


def test_a_review_failed_never_miss_archive_raises(world):
    mutator, labels = SpyMutator(), Labels()
    with world() as session:
        _decision(session).review_state = "review_failed"
        session.flush()
        with pytest.raises(NotReviewedError):
            archive_to_never_miss_label(
                session, USER_ID, "dec-nm", mutator=mutator, label_lookup=labels,
                dry_run=False,
            )
        assert mutator.calls == []


def test_there_is_no_force_parameter_to_bypass_the_review_gate():
    """`force=True` was never a way around `NotReviewedError`, and the new
    entry point does not even accept the argument."""
    import inspect

    signature = inspect.signature(archive_to_never_miss_label)
    assert "force" not in signature.parameters

    signature = inspect.signature(relabel_decision)
    assert "force" not in signature.parameters


def test_force_true_does_not_bypass_not_reviewed_on_apply_decision(world):
    """The pinned invariant, re-asserted here because Phase 9 mutates far more mail."""
    mutator, labels = SpyMutator(), Labels()
    with world() as session:
        _decision(session).review_state = "provisional"
        session.flush()
        with pytest.raises(NotReviewedError):
            apply_decision(
                session, USER_ID, "dec-nm", mutator=mutator, label_lookup=labels,
                dry_run=False, force=True,
            )
        assert mutator.calls == []


def test_needs_your_call_can_never_be_never_miss_archived(world):
    mutator, labels = SpyMutator(), Labels()
    with world() as session:
        _decision(session).status = "needs_your_call"
        session.flush()
        with pytest.raises(NeedsYourCallError):
            archive_to_never_miss_label(
                session, USER_ID, "dec-nm", mutator=mutator, label_lookup=labels,
                dry_run=False,
            )
        assert mutator.calls == []


def test_a_keep_proposal_is_never_never_miss_archived(world):
    from tools.actions import NotArchivableError

    mutator, labels = SpyMutator(), Labels()
    with world() as session:
        _decision(session).proposed_action = "keep"
        session.flush()
        with pytest.raises(NotArchivableError):
            archive_to_never_miss_label(
                session, USER_ID, "dec-nm", mutator=mutator, label_lookup=labels,
                dry_run=False,
            )
        assert mutator.calls == []


def test_a_never_miss_archive_is_undoable_and_undo_is_idempotent(world):
    mutator, labels = SpyMutator(), Labels()
    with world() as session:
        log = archive_to_never_miss_label(
            session, USER_ID, "dec-nm", mutator=mutator, label_lookup=labels,
            dry_run=False,
        )
        session.commit()
        log_id = log.id

    with world() as session:
        undo_action(session, USER_ID, log_id, mutator=mutator)
        session.commit()
        from db.models import Item

        assert session.get(Item, "it-nm").channel_labels == ["INBOX"]
        assert _decision(session).status == "undone"

    calls_before = len(mutator.calls)
    with world() as session:
        undo_action(session, USER_ID, log_id, mutator=mutator)
        session.commit()
    assert len(mutator.calls) == calls_before, "a second undo must call Gmail zero times"


# --------------------------------------------------------------------------
# relabel_decision
# --------------------------------------------------------------------------


def _archived_state(session, mutator):
    """Put the thread in the post-triage state: archived under ZeroInbox/Urgent."""
    from db.models import Category, Item

    session.get(Category, "cat-urgent").channel_label_id = "Label_ZeroInbox_Urgent"
    session.get(Category, "cat-important").channel_label_id = "Label_ZeroInbox_Important"
    item = session.get(Item, "it-nm")
    item.channel_labels = ["Label_ZeroInbox_Urgent"]
    mutator.labels["thread-nm"] = ["Label_ZeroInbox_Urgent"]
    decision = _decision(session)
    decision.status = "applied"
    decision.category_id = "cat-important"
    session.flush()


def test_relabel_keeps_an_archived_thread_archived(world):
    """`keep_archived=True` must NEVER re-add INBOX. Re-filing mail can never
    resurrect it into the inbox the user just cleared."""
    mutator, labels = SpyMutator(), Labels()
    with world() as session:
        _archived_state(session, mutator)
        log = relabel_decision(
            session, USER_ID, "dec-nm", mutator=mutator, label_lookup=labels,
            dry_run=False, keep_archived=True,
        )
        session.commit()

        call = mutator.calls[-1]
        assert call[0] == "restore_labels"
        assert call[2] == ("Label_ZeroInbox_Important",)
        assert call[3] == ("Label_ZeroInbox_Urgent",)
        assert "INBOX" not in call[2], "keep_archived=True must never re-add INBOX"

        from db.models import Item

        assert session.get(Item, "it-nm").channel_labels == ["Label_ZeroInbox_Important"]
        assert log.operation == "add_label"
        assert log.undo_token["original_label_ids"] == ["Label_ZeroInbox_Urgent"]


def test_relabel_with_keep_archived_false_returns_the_thread_to_the_inbox(world):
    mutator, labels = SpyMutator(), Labels()
    with world() as session:
        _archived_state(session, mutator)
        relabel_decision(
            session, USER_ID, "dec-nm", mutator=mutator, label_lookup=labels,
            dry_run=False, keep_archived=False,
        )
        session.commit()
        assert "INBOX" in mutator.calls[-1][2]


def test_relabel_never_removes_a_label_this_system_does_not_own(world):
    """A label the user made by hand is theirs. We only ever move our own."""
    mutator, labels = SpyMutator(), Labels()
    with world() as session:
        _archived_state(session, mutator)
        mutator.labels["thread-nm"] = ["Label_ZeroInbox_Urgent", "Label_MyOwnThing"]
        relabel_decision(
            session, USER_ID, "dec-nm", mutator=mutator, label_lookup=labels,
            dry_run=False, keep_archived=True,
        )
        session.commit()
        assert "Label_MyOwnThing" not in mutator.calls[-1][3]
        from db.models import Item

        assert "Label_MyOwnThing" in session.get(Item, "it-nm").channel_labels


def test_dry_run_blocks_relabel_absolutely(world):
    mutator, labels = SpyMutator(), Labels()
    with world() as session:
        _archived_state(session, mutator)
        with pytest.raises(DryRunViolation):
            relabel_decision(
                session, USER_ID, "dec-nm", mutator=mutator, label_lookup=labels,
                dry_run=True, keep_archived=True,
            )
        assert mutator.calls == []


def test_relabel_refuses_an_unreviewed_row_before_the_mutator(world):
    mutator, labels = SpyMutator(), Labels()
    with world() as session:
        _archived_state(session, mutator)
        _decision(session).review_state = "provisional"
        session.flush()
        with pytest.raises(NotReviewedError):
            relabel_decision(
                session, USER_ID, "dec-nm", mutator=mutator, label_lookup=labels,
                dry_run=False, keep_archived=True,
            )
        assert mutator.calls == []


def test_relabel_refuses_a_row_that_was_never_approved_or_applied(world):
    mutator, labels = SpyMutator(), Labels()
    with world() as session:
        _archived_state(session, mutator)
        _decision(session).status = "proposed"
        session.flush()
        with pytest.raises(NotApprovedError):
            relabel_decision(
                session, USER_ID, "dec-nm", mutator=mutator, label_lookup=labels,
                dry_run=False, keep_archived=True,
            )
        assert mutator.calls == []


def test_every_operation_this_module_writes_is_non_destructive(world):
    """Trust invariant: archive / add_label / remove_label, and nothing else, ever."""
    mutator, labels = SpyMutator(), Labels()
    with world() as session:
        archive_to_never_miss_label(
            session, USER_ID, "dec-nm", mutator=mutator, label_lookup=labels,
            dry_run=False,
        )
        session.commit()

    with world() as session:
        _archived_state(session, mutator)
        relabel_decision(
            session, USER_ID, "dec-nm", mutator=mutator, label_lookup=labels,
            dry_run=False, keep_archived=True,
        )
        session.commit()

        from sqlalchemy import select

        from db.models import ActionLog

        logs = list(session.execute(select(ActionLog)).scalars())
        assert logs
        assert {log.operation for log in logs} <= {"archive", "add_label", "remove_label"}
        assert all(log.undo_token for log in logs)

    assert not hasattr(mutator, "trash"), "there is no trash/delete/spam verb, by design"
    from channels.gmail.mutations import GmailMutator

    for forbidden in ("trash", "delete", "spam", "report_spam", "delete_thread"):
        assert not hasattr(GmailMutator, forbidden), (
            f"GmailMutator.{forbidden} must not exist — the prohibition is structural"
        )
