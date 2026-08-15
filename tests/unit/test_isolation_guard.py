"""The isolation guard itself must be falsifiable — these tests attack it.

Runnable immediately (no app imports): they prove the guard refuses the real
database, the real account, the real mailbox in every Gmail spelling, and a
live Gmail write — and that legitimate synthetic test data passes.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine

from tests.isolation import (
    RealDatabaseError,
    RealGmailError,
    RealUserError,
    assert_isolated_db,
    assert_not_real_gmail_write,
    assert_test_mailbox,
    assert_test_user,
    is_denied_email,
    is_test_user_id,
    normalise_gmail,
)


class TestDatabaseGuard:
    def test_tmp_path_file_is_allowed(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path}/ok.db")
        assert_isolated_db(engine)  # must not raise

    def test_production_agent_db_is_refused(self):
        with pytest.raises(RealDatabaseError, match="agent.db"):
            assert_isolated_db(create_engine("sqlite:///./data/agent.db"))

    def test_old_design_zero_inbox_db_is_refused(self):
        with pytest.raises(RealDatabaseError):
            assert_isolated_db(create_engine("sqlite:///./zero_inbox.db"))

    def test_any_file_under_data_dir_is_refused(self):
        with pytest.raises(RealDatabaseError, match="data/"):
            assert_isolated_db(create_engine("sqlite:///./data/scratch.db"))

    def test_in_memory_is_refused_because_the_fixture_never_builds_it(self):
        with pytest.raises(RealDatabaseError):
            assert_isolated_db(create_engine("sqlite://"))

    def test_the_autouse_fixture_bound_a_throwaway_db(self, _isolated_db):
        assert_isolated_db(_isolated_db)


class TestAccountGuard:
    def test_reserved_test_id_passes(self):
        assert is_test_user_id("test-user-alice")
        assert_test_user("test-user-alice")

    def test_denied_real_id_is_refused_even_with_test_prefix(self):
        with pytest.raises(RealUserError, match="REAL ACCOUNT"):
            assert_test_user("test-6b4ab0f4-dead-beef-0000-000000000000")

    def test_bare_uuid4_is_refused_as_a_copied_production_id(self):
        with pytest.raises(RealUserError):
            assert_test_user("9a1b2c3d-4e5f-4a6b-8c7d-0e1f2a3b4c5d")

    def test_unprefixed_id_is_refused(self):
        with pytest.raises(RealUserError):
            assert_test_user("alice")


class TestMailboxGuard:
    def test_example_com_passes(self):
        assert_test_mailbox("alice@example.com")

    def test_real_mailbox_refused_in_every_gmail_spelling(self):
        for spelling in (
            "psykrsna@gmail.com",
            "psy.krsna@gmail.com",
            "psykrsna+zero@gmail.com",
            "PsyKrsna@googlemail.com",
        ):
            assert is_denied_email(spelling), spelling
            with pytest.raises(RealUserError):
                assert_test_mailbox(spelling)

    def test_routable_domain_is_refused(self):
        with pytest.raises(RealUserError):
            assert_test_mailbox("someone@gmail.com")

    def test_normalisation_only_folds_gmail_domains(self):
        assert normalise_gmail("A.b+c@gmail.com") == "ab@gmail.com"
        assert normalise_gmail("a.b@example.com") == "a.b@example.com"


class TestGmailWriteGuard:
    def test_gmail_write_is_refused(self):
        with pytest.raises(RealGmailError, match="live Gmail WRITE"):
            assert_not_real_gmail_write(
                "POST",
                "https://gmail.googleapis.com/gmail/v1/users/me/threads/abc/modify",
            )

    def test_label_create_is_refused(self):
        with pytest.raises(RealGmailError):
            assert_not_real_gmail_write(
                "POST", "https://gmail.googleapis.com/gmail/v1/users/me/labels"
            )

    def test_gmail_read_stays_real(self):
        assert_not_real_gmail_write(
            "GET", "https://gmail.googleapis.com/gmail/v1/users/me/threads?labelIds=INBOX"
        )

    def test_oauth_token_refresh_is_untouched(self):
        assert_not_real_gmail_write("POST", "https://oauth2.googleapis.com/token")


class TestIsolationFlag:
    def test_the_choke_point_flag_is_exported_for_every_test(self):
        # Layer 1 of the guard: the mutation choke point contract.
        assert os.environ.get("AGENT_TEST_ISOLATION") == "1"


class TestRowGuard:
    def test_guarded_row_for_real_account_refused_at_flush(self, _isolated_db):
        """The before_flush listener fires on the write, not at teardown."""
        from sqlalchemy.orm import Session

        from db import models

        with Session(_isolated_db) as session:
            row = models.Category(
                id="test-cat-x",
                user_id="6b4ab0f4-0000-0000-0000-000000000000",
                name="leaked",
                description="",
                rule="label_only",
                position=0,
            )
            session.add(row)
            with pytest.raises(RealUserError, match="REAL ACCOUNT"):
                session.commit()
