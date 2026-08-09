"""VIP CRUD, priorities profile, and correction recording — signal only, no rule writes.

Phase 2 stops at recording the correction signal: see spec/capabilities/user-memory.md.
No test here, nor any code path in src/tools/memory.py, may write a `rules` row.
"""

import pytest
from sqlalchemy import select

from db.models import Correction, Item, Rule, SenderProfile, User
from tools.memory import (
    add_vip_entry,
    get_priority_profile,
    is_vip,
    list_corrections,
    list_vip_entries,
    record_correction,
    remove_vip_entry,
    set_priority_profile,
)


@pytest.fixture
def db_session(_isolated_db):
    from db.session import create_db_session

    with create_db_session() as session:
        yield session


def _make_user(session, email="u@example.com") -> str:
    user = User(email=email, display_name="Test User")
    session.add(user)
    session.flush()
    return user.id


def _make_item(session, user_id, from_email="sender@corp.com", from_domain="corp.com") -> str:
    item = Item(
        user_id=user_id,
        channel_account_id="chacc-1",
        external_thread_id="thread-1",
        subject="Re: quarterly update",
        from_name="Sender",
        from_email=from_email,
        from_domain=from_domain,
        snippet_redacted="hello there",
    )
    session.add(item)
    session.flush()
    return item.id


# --- VIP CRUD -------------------------------------------------------------------


def test_vip_add_and_list(db_session):
    user_id = _make_user(db_session)
    entry = add_vip_entry(db_session, user_id, "email", "vip@example.com")
    assert entry.id
    entries = list_vip_entries(db_session, user_id)
    assert len(entries) == 1
    assert entries[0].value == "vip@example.com"


def test_vip_remove(db_session):
    user_id = _make_user(db_session)
    entry = add_vip_entry(db_session, user_id, "domain", "investors.com")
    assert remove_vip_entry(db_session, user_id, entry.id) is True
    assert list_vip_entries(db_session, user_id) == []


def test_vip_remove_missing_returns_false(db_session):
    user_id = _make_user(db_session)
    assert remove_vip_entry(db_session, user_id, "nonexistent-id") is False


def test_vip_invalid_kind_rejected(db_session):
    user_id = _make_user(db_session)
    with pytest.raises(ValueError):
        add_vip_entry(db_session, user_id, "phone_number", "555-1234")


def test_vip_empty_value_rejected(db_session):
    user_id = _make_user(db_session)
    with pytest.raises(ValueError):
        add_vip_entry(db_session, user_id, "keyword", "   ")


def test_vip_is_vip_matches_domain_and_keyword(db_session):
    user_id = _make_user(db_session)
    add_vip_entry(db_session, user_id, "domain", "investors.com")
    add_vip_entry(db_session, user_id, "keyword", "fundraise")
    assert is_vip(db_session, user_id, email="a@sub.investors.com", domain="sub.investors.com")
    assert is_vip(db_session, user_id, subject="Update on our fundraise")
    assert not is_vip(db_session, user_id, email="a@random.com", domain="random.com", subject="hi")


def test_vip_scoped_per_user(db_session):
    user_a = _make_user(db_session, "a@example.com")
    user_b = _make_user(db_session, "b@example.com")
    add_vip_entry(db_session, user_a, "email", "shared@example.com")
    assert list_vip_entries(db_session, user_b) == []


# --- Priorities profile ----------------------------------------------------------


def test_profile_set_and_get(db_session):
    user_id = _make_user(db_session)
    assert get_priority_profile(db_session, user_id) == ""
    set_priority_profile(db_session, user_id, "I care about investors and our fundraise.")
    assert get_priority_profile(db_session, user_id) == "I care about investors and our fundraise."


def test_profile_overwrite(db_session):
    user_id = _make_user(db_session)
    set_priority_profile(db_session, user_id, "first version")
    set_priority_profile(db_session, user_id, "second version")
    assert get_priority_profile(db_session, user_id) == "second version"


# --- Corrections: signal only, never a rules row ----------------------------------


def test_correction_records_row_and_raises_importance(db_session):
    user_id = _make_user(db_session)
    item_id = _make_item(db_session, user_id, from_email="founder@investors.com")

    correction = record_correction(
        db_session,
        user_id,
        item_id=item_id,
        from_action="archive",
        to_action="keep",
        source="observed_unarchive",
    )
    assert correction.id
    assert correction.from_action == "archive"
    assert correction.to_action == "keep"

    corrections = list_corrections(db_session, user_id)
    assert len(corrections) == 1

    profile = db_session.execute(
        select(SenderProfile).where(
            SenderProfile.user_id == user_id,
            SenderProfile.sender_email == "founder@investors.com",
        )
    ).scalar_one()
    assert profile.importance_score > 0.0


def test_correction_repeated_accumulates_importance(db_session):
    user_id = _make_user(db_session)
    item_id = _make_item(db_session, user_id, from_email="repeat@corp.com")

    record_correction(db_session, user_id, item_id=item_id, from_action="archive", to_action="keep")
    record_correction(db_session, user_id, item_id=item_id, from_action="archive", to_action="keep")
    record_correction(db_session, user_id, item_id=item_id, from_action="archive", to_action="keep")

    profile = db_session.execute(
        select(SenderProfile).where(
            SenderProfile.user_id == user_id,
            SenderProfile.sender_email == "repeat@corp.com",
        )
    ).scalar_one()

    corrections = list_corrections(db_session, user_id)
    assert len(corrections) == 3
    assert profile.importance_score > 0.0

    # Phase 2 hard rule: three corrections against the same sender never create
    # a rules row — that is Phase 3's rule miner's job, reading this table later.
    rules = db_session.execute(select(Rule).where(Rule.user_id == user_id)).scalars().all()
    assert rules == []


def test_correction_missing_item_raises(db_session):
    user_id = _make_user(db_session)
    with pytest.raises(ValueError):
        record_correction(
            db_session,
            user_id,
            item_id="nonexistent-item",
            from_action="archive",
            to_action="keep",
        )


def test_correction_never_writes_rules_table_globally(db_session):
    """Across every code path in tools/memory.py, no rules row is ever created."""
    user_id = _make_user(db_session)
    item_id = _make_item(db_session, user_id)
    add_vip_entry(db_session, user_id, "email", "vip@example.com")
    set_priority_profile(db_session, user_id, "prioritize investors")
    record_correction(db_session, user_id, item_id=item_id, from_action="archive", to_action="keep")

    assert db_session.execute(select(Rule)).scalars().all() == []
    assert len(db_session.execute(select(Correction)).scalars().all()) == 1


def test_correction_scoped_per_user_second_user_unaffected(db_session):
    user_a = _make_user(db_session, "a@example.com")
    user_b = _make_user(db_session, "b@example.com")
    item_a = _make_item(db_session, user_a, from_email="shared-sender@corp.com")

    record_correction(db_session, user_a, item_id=item_a, from_action="archive", to_action="keep")

    profile_b = db_session.execute(
        select(SenderProfile).where(
            SenderProfile.user_id == user_b,
            SenderProfile.sender_email == "shared-sender@corp.com",
        )
    ).scalar_one_or_none()
    assert profile_b is None
