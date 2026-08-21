"""Slice db-and-domain — schema tests for spec/data.md.

Runs against the isolated throwaway SQLite DB provided by tests/conftest.py.
"""

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from db.models import (
    Base,
    Category,
    GmailAccount,
    InboxSnapshot,
    LlmCall,
    Mutation,
    Run,
    RunEvent,
    SenderProfile,
    ThreadDecision,
    User,
)

EXPECTED_TABLES = {
    "users",
    "gmail_accounts",
    "categories",
    "runs",
    "thread_decisions",
    "mutations",
    "run_events",
    "llm_calls",
    "sender_profiles",
    "inbox_snapshots",
}


@pytest.fixture
def session(_isolated_db):
    factory = sessionmaker(bind=_isolated_db, autoflush=False)
    with factory() as s:
        yield s


def _user(session: Session, tag: str) -> User:
    user = User(id=f"test-user-{tag}", email=f"{tag}@example.test", name=tag)
    session.add(user)
    session.commit()
    return user


def _category(session: Session, user: User, name: str = "Finance", **kw) -> Category:
    cat = Category(user_id=user.id, name=name, description="money things", **kw)
    session.add(cat)
    session.commit()
    return cat


def _run(session: Session, user: User) -> Run:
    run = Run(user_id=user.id)
    session.add(run)
    session.commit()
    return run


class TestSchema:
    def test_all_spec_tables_exist(self, _isolated_db):
        assert EXPECTED_TABLES <= set(inspect(_isolated_db).get_table_names())
        assert EXPECTED_TABLES == set(Base.metadata.tables)

    def test_every_table_except_users_is_user_keyed_and_indexed(self, _isolated_db):
        inspector = inspect(_isolated_db)
        for table in EXPECTED_TABLES - {"users"}:
            columns = {c["name"] for c in inspector.get_columns(table)}
            assert "user_id" in columns, f"{table} is missing user_id"
            indexed = {col for ix in inspector.get_indexes(table) for col in ix["column_names"]}
            unique_cols = {
                col for uc in inspector.get_unique_constraints(table) for col in uc["column_names"]
            }
            assert "user_id" in indexed | unique_cols, f"{table}.user_id not indexed"


class TestDefaultsAndLifecycles:
    def test_run_defaults_match_spec(self, session):
        user = _user(session, "rundef")
        run = _run(session, user)
        assert run.status == "running"
        assert run.trigger == "clean_chunk"
        assert run.chunk_limit == 50
        assert run.threads_decided == 0
        assert run.counts_json == {}
        assert run.est_cost_usd == 0.0
        assert run.finished_at is None and run.undone_at is None

    def test_gmail_account_defaults_connected(self, session):
        user = _user(session, "gm")
        acct = GmailAccount(
            user_id=user.id, google_email="gm@example.test", refresh_token_encrypted="enc"
        )
        session.add(acct)
        session.commit()
        assert acct.status == "connected"
        assert acct.connected_at is not None

    def test_thread_decision_row_roundtrip(self, session):
        user = _user(session, "td")
        cat = _category(session, user)
        run = _run(session, user)
        decision = ThreadDecision(
            user_id=user.id,
            run_id=run.id,
            gmail_thread_id="t-1",
            sender="Acme <billing@acme.test>",
            subject="Your invoice",
            snippet="Invoice #42 attached...",
            category_id=cat.id,
            confidence=0.91,
            reason="billing sender, invoice subject",
        )
        session.add(decision)
        session.commit()
        assert decision.needs_review is False
        assert decision.source == "llm"
        assert decision.undone is False

    def test_snapshot_json_columns_roundtrip(self, session):
        user = _user(session, "snap")
        snap = InboxSnapshot(
            user_id=user.id,
            total_inbox_threads=120,
            unread=30,
            oldest_days=400,
            top_senders_json=[{"address": "a@b.test", "count": 12}],
            category_tab_counts_json={"promotions": 60},
        )
        session.add(snap)
        session.commit()
        session.expire_all()
        stored = session.get(InboxSnapshot, snap.id)
        assert stored.top_senders_json[0]["count"] == 12
        assert stored.category_tab_counts_json == {"promotions": 60}


class TestConstraints:
    def test_one_gmail_account_per_user(self, session):
        user = _user(session, "onegm")
        for i in range(2):
            session.add(
                GmailAccount(
                    user_id=user.id, google_email="x@example.test", refresh_token_encrypted="e"
                )
            )
            if i == 0:
                session.commit()
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    def test_thread_decision_unique_per_user_but_not_across_users(self, session):
        user_a = _user(session, "a")
        user_b = _user(session, "b")
        cat_a = _category(session, user_a)
        cat_b = _category(session, user_b)
        run_a = _run(session, user_a)
        run_b = _run(session, user_b)

        def decision(user, run, cat):
            return ThreadDecision(
                user_id=user.id,
                run_id=run.id,
                gmail_thread_id="same-thread",
                sender="s@example.test",
                category_id=cat.id,
            )

        session.add(decision(user_a, run_a, cat_a))
        session.add(decision(user_b, run_b, cat_b))  # same thread id, other user: fine
        session.commit()

        session.add(decision(user_a, run_a, cat_a))  # duplicate within a user: refused
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    def test_category_name_unique_per_user(self, session):
        user = _user(session, "cats")
        _category(session, user, "Finance")
        session.add(Category(user_id=user.id, name="Finance"))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    def test_run_event_seq_unique_per_run(self, session):
        user = _user(session, "ev")
        run = _run(session, user)
        session.add(
            RunEvent(user_id=user.id, run_id=run.id, seq=1, type="decision", sentence="Filed x.")
        )
        session.commit()
        session.add(
            RunEvent(user_id=user.id, run_id=run.id, seq=1, type="action", sentence="dup seq")
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    def test_sender_profile_unique_per_user_sender(self, session):
        user = _user(session, "sp")
        cat = _category(session, user)
        session.add(
            SenderProfile(user_id=user.id, sender_address="gh@example.test", category_id=cat.id)
        )
        session.commit()
        session.add(
            SenderProfile(user_id=user.id, sender_address="gh@example.test", category_id=cat.id)
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


class TestMultiUserIsolationShape:
    def test_rows_are_partitioned_by_user_id(self, session):
        """Two users' full object graphs coexist; filtering by user_id splits them."""
        results = {}
        for tag in ("iso1", "iso2"):
            user = _user(session, tag)
            cat = _category(session, user)
            run = _run(session, user)
            session.add_all(
                [
                    Mutation(
                        user_id=user.id,
                        run_id=run.id,
                        gmail_thread_id=f"{tag}-t1",
                        action="add_label",
                        label_name="Finance",
                        reason="filed",
                    ),
                    LlmCall(
                        user_id=user.id,
                        run_id=run.id,
                        provider="nvidia",
                        model="nvidia/nemotron-3-nano-30b-a3b",
                        tokens_in=100,
                        tokens_out=20,
                    ),
                ]
            )
            session.commit()
            results[tag] = user.id
        for model in (Category, Run, Mutation, LlmCall):
            for user_id in results.values():
                rows = session.query(model).filter(model.user_id == user_id).all()
                assert len(rows) == 1
                assert all(r.user_id == user_id for r in rows)
