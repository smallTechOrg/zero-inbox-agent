"""apply_decision emits thread_archived SSE after a successful Gmail mutation."""

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

    def get_thread_labels(self, thread_id: str) -> list[str]:
        return []


class FakeLabelLookup:
    def ensure_label(self, name: str) -> dict:
        return {"id": "Label_1", "name": name}


@pytest.fixture
def seeded(_isolated_db):
    from db.models import Category, ChannelAccount, Decision, Item, TriageRun, User, UserSettings
    from db.session import create_db_session

    with create_db_session() as session:
        session.add(User(id="u2", email="u2@example.com", display_name="U2"))
        session.add(UserSettings(user_id="u2"))
        session.add(
            ChannelAccount(
                id="acct2",
                user_id="u2",
                channel="gmail",
                account_email="u2@gmail.com",
                refresh_token_enc="ENC",
                scopes=["gmail.readonly"],
                status="connected",
            )
        )
        session.add(
            Category(
                id="cat2",
                user_id="u2",
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
                id="run2",
                user_id="u2",
                channel_account_id="acct2",
                status="completed",
                dry_run=False,
                items_total=1,
                items_decided=1,
            )
        )
        item = Item(
            id="item_a",
            user_id="u2",
            channel_account_id="acct2",
            external_thread_id="thread_a",
            external_message_ids=["msg_a"],
            subject="Weekly Digest",
            from_name="Sender",
            from_email="s@example.com",
            from_domain="example.com",
            message_count=1,
            snippet_redacted="digest",
            internal_date=datetime.now(timezone.utc),
            is_unread=False,
        )
        session.add(item)
        decision = Decision(
            id="dec_a",
            user_id="u2",
            item_id="item_a",
            run_id="run2",
            category_id="cat2",
            proposed_action="archive",
            confidence=0.95,
            reasoning="r",
            decided_by="llm",
            time_sensitive=False,
            status="approved",
        )
        session.add(decision)
        session.commit()

    return {"decision_id": "dec_a"}


def test_thread_archived_emitted_after_successful_apply(seeded, monkeypatch):
    """apply_decision emits type='thread_archived' with correct fields."""
    import events.bus as bus_mod
    from db.session import create_db_session
    from tools.actions import apply_decision

    emitted: list[dict] = []
    monkeypatch.setattr(bus_mod, "emit", lambda uid, evt: emitted.append(evt))

    mutator = FakeMutator()
    with create_db_session() as session:
        apply_decision(
            session,
            "u2",
            seeded["decision_id"],
            mutator=mutator,
            label_lookup=FakeLabelLookup(),
            dry_run=False,
        )
        session.commit()

    archived_events = [e for e in emitted if e.get("type") == "thread_archived"]
    assert len(archived_events) >= 1
    evt = archived_events[0]
    assert evt["subject"] == "Weekly Digest"
    assert evt["category"] == "Newsletters"
    assert evt["run_id"] == "run2"
    assert "label_name" in evt
    assert "ts" in evt
