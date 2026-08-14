"""apply_decision must never mutate a `keep`-proposed decision.

Regression: a bulk "approve all" call marks EVERY decision in a run
`approved` — including `keep` proposals, which just means "yes, this stays
in my inbox," not "archive this." apply_decision had no check on
`proposed_action` at all, so a blanket apply over every approved id would
have archived confidently-important mail (real people, urgent) exactly
opposite of the never-miss guarantee. Caught live, before it touched a real
1170-thread batch, by inspecting the code rather than trusting the bulk
endpoint's "approved" status as sufficient license to mutate.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


class FakeMutator:
    def __init__(self):
        self.archived: list[str] = []

    def archive_and_label(self, thread_id: str, *, category_label_id: str) -> dict:
        self.archived.append(thread_id)
        return {"id": thread_id}

    def undo_archive_and_label(self, thread_id: str, *, category_label_id: str) -> dict:
        return {"id": thread_id}


class FakeLabelLookup:
    def ensure_label(self, name: str) -> dict:
        return {"id": "Label_1", "name": name}


@pytest.fixture
def seeded(_isolated_db):
    from db.models import Category, ChannelAccount, Decision, Item, TriageRun, User, UserSettings
    from db.session import create_db_session

    with create_db_session() as session:
        session.add(User(id="u1", email="u1@example.com", display_name="U1"))
        session.add(UserSettings(user_id="u1"))
        session.add(
            ChannelAccount(
                id="acct1",
                user_id="u1",
                channel="gmail",
                account_email="u1@gmail.com",
                refresh_token_enc="ENC",
                scopes=["gmail.readonly"],
                status="connected",
            )
        )
        session.add(
            Category(
                id="cat1",
                user_id="u1",
                key="people",
                name="People",
                description="",
                channel_label_name="ZeroInbox/People",
                default_action="keep",
                is_default=True,
                sort_order=1,
            )
        )
        session.add(
            TriageRun(
                id="run1",
                user_id="u1",
                channel_account_id="acct1",
                status="completed",
                dry_run=False,
                items_total=2,
                items_decided=2,
            )
        )

        def _item_and_decision(idx: int, proposed_action: str) -> str:
            item = Item(
                id=f"item{idx}",
                user_id="u1",
                channel_account_id="acct1",
                external_thread_id=f"thread{idx}",
                external_message_ids=[f"msg{idx}"],
                subject="s",
                from_name="n",
                from_email="e@example.com",
                from_domain="example.com",
                message_count=1,
                snippet_redacted="s",
                internal_date=datetime.now(timezone.utc),
                is_unread=False,
            )
            session.add(item)
            decision = Decision(
                id=f"dec{idx}",
                user_id="u1",
                item_id=item.id,
                run_id="run1",
                category_id="cat1",
                proposed_action=proposed_action,
                confidence=0.9,
                reasoning="r",
                decided_by="llm",
                time_sensitive=False,
                status="approved",
                # Phase 6: only a reviewed decision is appliable at all — this fixture
                # exercises the proposed_action/status guards, not the review gate.
                review_state="reviewed",
            )
            session.add(decision)
            return decision.id

        keep_id = _item_and_decision(1, "keep")
        archive_id = _item_and_decision(2, "archive")
        session.commit()

    return {"keep_id": keep_id, "archive_id": archive_id}


def test_apply_decision_refuses_a_keep_proposed_decision_even_when_approved(seeded):
    from db.session import create_db_session
    from tools.actions import NotArchivableError, apply_decision

    mutator = FakeMutator()
    with create_db_session() as session:
        with pytest.raises(NotArchivableError):
            apply_decision(
                session,
                "u1",
                seeded["keep_id"],
                mutator=mutator,
                label_lookup=FakeLabelLookup(),
                dry_run=False,
            )
    assert mutator.archived == []


def test_apply_decision_still_archives_an_archive_proposed_decision(seeded):
    from db.session import create_db_session
    from tools.actions import apply_decision

    mutator = FakeMutator()
    with create_db_session() as session:
        action_log = apply_decision(
            session,
            "u1",
            seeded["archive_id"],
            mutator=mutator,
            label_lookup=FakeLabelLookup(),
            dry_run=False,
        )
        undo_token = action_log.undo_token
        session.commit()
    assert undo_token is not None
    assert mutator.archived == ["thread2"]


def test_force_true_deliberately_overrides_the_keep_guard(seeded):
    """The only sanctioned way to archive a 'keep' decision: an explicit,
    per-call force=True — never the default path."""
    from db.session import create_db_session
    from tools.actions import apply_decision

    mutator = FakeMutator()
    with create_db_session() as session:
        action_log = apply_decision(
            session,
            "u1",
            seeded["keep_id"],
            mutator=mutator,
            label_lookup=FakeLabelLookup(),
            dry_run=False,
            force=True,
        )
        undo_token = action_log.undo_token
        session.commit()
    assert undo_token is not None
    assert mutator.archived == ["thread1"]


def test_the_keep_decision_status_is_unchanged_after_the_refused_apply(seeded):
    from db.models import Decision
    from db.session import create_db_session
    from tools.actions import NotArchivableError, apply_decision

    with create_db_session() as session:
        try:
            apply_decision(
                session,
                "u1",
                seeded["keep_id"],
                mutator=FakeMutator(),
                label_lookup=FakeLabelLookup(),
                dry_run=False,
            )
        except NotArchivableError:
            session.rollback()

    with create_db_session() as session:
        decision = session.get(Decision, seeded["keep_id"])
        assert decision.status == "approved"  # never flipped to "applied"


def test_force_does_not_override_needs_your_call_or_not_approved(seeded):
    """force only ever overrides the keep-proposed check — the two other hard
    rules (needs_your_call, not-approved) are absolute regardless of force."""
    from db.models import Decision
    from db.session import create_db_session
    from tools.actions import NeedsYourCallError, NotApprovedError, apply_decision

    with create_db_session() as session:
        decision = session.get(Decision, seeded["keep_id"])
        decision.status = "needs_your_call"

    with create_db_session() as session:
        with pytest.raises(NeedsYourCallError):
            apply_decision(
                session,
                "u1",
                seeded["keep_id"],
                mutator=FakeMutator(),
                label_lookup=FakeLabelLookup(),
                dry_run=False,
                force=True,
            )

    with create_db_session() as session:
        decision = session.get(Decision, seeded["archive_id"])
        decision.status = "proposed"  # not approved

    with create_db_session() as session:
        with pytest.raises(NotApprovedError):
            apply_decision(
                session,
                "u1",
                seeded["archive_id"],
                mutator=FakeMutator(),
                label_lookup=FakeLabelLookup(),
                dry_run=False,
                force=True,
            )
