"""The isolation guard is itself tested — it is the slice's whole deliverable.

Phase 9, slice 8. The ``e2e-actions-test`` category on the user's live account
was written by a test that was *able* to bind production. These tests are the
proof that it can no longer happen: each one performs a deliberate escape
attempt and asserts the guard stops it. **They fail if the guard is removed or
weakened**, which is the point.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tests.isolation import (
    RealDatabaseError,
    RealGmailError,
    RealUserError,
    assert_isolated_db,
    assert_not_real_gmail_request,
    assert_test_mailbox,
    assert_test_user,
    is_denied_email,
    is_test_user_id,
    looks_like_real_account_id,
    normalise_gmail,
)

REAL_USER_ID = "6b4ab0f4-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
REAL_EMAIL = "psykrsna@gmail.com"


# --- escape attempt 1: write a Category onto the real account ---------------


def test_writing_a_category_for_the_real_user_raises(_isolated_db):
    """The exact write that left 'e2e-actions-test' on the live taxonomy."""
    from db.models import Category

    session = sessionmaker(bind=_isolated_db)()
    session.add(
        Category(
            user_id=REAL_USER_ID,
            key="e2e-actions-test",
            name="E2EActionsTest",
            default_action="archive",
        )
    )
    with pytest.raises(RealUserError) as exc:
        session.commit()
    session.rollback()

    message = str(exc.value)
    assert REAL_USER_ID in message, "the guard must name the offending row"
    assert "categories" in message
    assert "REAL ACCOUNT" in message


def test_the_real_account_is_refused_on_every_guarded_table(_isolated_db):
    """Not just categories — a decision or a rule for the real user is refused too."""
    from db.models import Decision, Rule

    for row in (
        Decision(user_id=REAL_USER_ID, item_id="t1", proposed_action="archive"),
        Rule(user_id=REAL_USER_ID, matcher={"from_email": "x@example.com"},
             action="archive"),
    ):
        session = sessionmaker(bind=_isolated_db)()
        session.add(row)
        with pytest.raises(RealUserError):
            session.commit()
        session.rollback()
        session.close()


def test_a_locally_generated_uuid_id_is_allowed(_isolated_db):
    """A uuid minted inside the isolated DB is ordinary test data, not production.

    Shape alone cannot separate the two, so the commit guard does not refuse it —
    the binding check is what keeps such an id off the real account. Documented
    here so a future change cannot 'tighten' this and break ~30 existing tests
    without noticing why it was left open.
    """
    import uuid

    from db.models import Category

    session = sessionmaker(bind=_isolated_db)()
    session.add(
        Category(user_id=str(uuid.uuid4()), key="receipts", name="Receipts",
                 default_action="keep")
    )
    session.commit()
    assert session.query(Category).count() == 1
    session.close()


def test_a_channel_account_for_the_real_mailbox_raises(_isolated_db):
    """Including the dotted / +tagged Gmail spellings of the same mailbox."""
    from db.models import ChannelAccount

    session = sessionmaker(bind=_isolated_db)()
    session.add(
        ChannelAccount(
            user_id="test-user",
            channel="gmail",
            account_email="psy.krsna+zero@gmail.com",
        )
    )
    with pytest.raises(RealUserError) as exc:
        session.commit()
    session.rollback()
    assert "REAL MAILBOX" in str(exc.value)


def test_a_synthetic_write_still_succeeds(_isolated_db):
    """The guard must not stand in the way of ordinary test data."""
    from db.models import Category

    session = sessionmaker(bind=_isolated_db)()
    session.add(
        Category(user_id="test-user", key="newsletters", name="Newsletters",
                 default_action="keep")
    )
    session.commit()
    assert session.query(Category).count() == 1
    session.close()


# --- escape attempt 2: bind the real database ------------------------------


def test_binding_the_real_database_raises():
    engine = create_engine("sqlite:///./zero_inbox.db")
    with pytest.raises(RealDatabaseError) as exc:
        assert_isolated_db(engine)
    assert "zero_inbox.db" in str(exc.value)
    engine.dispose()


def test_binding_anything_under_data_raises():
    engine = create_engine("sqlite:///./data/phase9_migration_check.db")
    with pytest.raises(RealDatabaseError):
        assert_isolated_db(engine)
    engine.dispose()


def test_a_session_on_a_real_database_engine_is_refused_at_flush_time(tmp_path):
    """The guard fires on the WRITE, not at teardown — the row never lands."""
    from db.models import Base, Category

    # A stand-in for zero_inbox.db that is safe to create: same filename, in a
    # scratch directory. The guard refuses it on name alone, so no real data is
    # ever opened by this test.
    real_ish = tmp_path / "zero_inbox.db"
    engine = create_engine(f"sqlite:///{real_ish}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(Category(user_id="test-user", key="k", name="K", default_action="keep"))
    with pytest.raises(RealDatabaseError) as exc:
        session.commit()
    session.rollback()
    assert "zero_inbox.db" in str(exc.value)
    engine.dispose()


def test_the_isolated_db_fixture_itself_passes_the_guard(_isolated_db):
    assert_isolated_db(_isolated_db)
    assert "test.db" in str(_isolated_db.url)


# --- escape attempt 3: a live Gmail mutation --------------------------------


def test_a_real_googleapiclient_request_is_refused():
    from googleapiclient.http import HttpRequest

    request = HttpRequest.__new__(HttpRequest)  # never executed; shape is the signal
    with pytest.raises(RealGmailError) as exc:
        assert_not_real_gmail_request(request)
    assert "real Gmail API request" in str(exc.value)


def test_the_mutator_refuses_a_real_request_but_runs_a_fake_one():
    """The guard is wired into GmailMutator._execute, not merely available."""
    from googleapiclient.http import HttpRequest

    from channels.gmail.mutations import GmailMutator

    class _FakeRequest:
        def execute(self):
            return {"labelIds": ["INBOX"]}

    mutator = GmailMutator(service=object())
    assert mutator._execute(_FakeRequest()) == {"labelIds": ["INBOX"]}

    with pytest.raises(RealGmailError):
        mutator._execute(HttpRequest.__new__(HttpRequest))


# --- the reserved-convention helpers ---------------------------------------


def test_assert_test_user_enforces_the_reserved_prefix():
    assert_test_user("test-user-alice")
    for bad in (REAL_USER_ID, "test-" + REAL_USER_ID, "user-alice", None, 7):
        with pytest.raises(RealUserError):
            assert_test_user(bad)


def test_assert_test_mailbox_enforces_non_routable_domains():
    assert_test_mailbox("alice@example.com")
    assert_test_mailbox("bob@test.invalid")
    for bad in (REAL_EMAIL, "someone@gmail.com", "not-an-address", None):
        with pytest.raises(RealUserError):
            assert_test_mailbox(bad)


def test_identity_helpers():
    assert is_test_user_id("test-user-alice")
    assert not is_test_user_id(REAL_USER_ID)
    assert looks_like_real_account_id(REAL_USER_ID)
    assert not looks_like_real_account_id("test-user-alice")
    assert normalise_gmail("Psy.Krsna+news@googlemail.com") == REAL_EMAIL
    assert is_denied_email("PSY.KRSNA+anything@gmail.com")
    assert not is_denied_email("alice@example.com")
