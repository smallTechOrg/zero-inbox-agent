"""The remainder ledger tells the truth about how far from zero the inbox is.

Covers the three-scenario minimum for `graph.remainder.remainder_ledger`:
happy path (every bucket populated), the edge case (a run with no decisions at
all, and NULL `autonomy_state` rows from before Phase 7), and the error path (a
run id belonging to another user, and an autonomy_state nobody recognises).
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from graph.remainder import REMAINDER_BUCKETS, remainder_ledger


@contextmanager
def capture_warnings(logger_name: str):
    """Collect WARNING records straight off *logger_name*.

    Not ``caplog``: the app configures logging at import time, so whether the root
    handler sees these records depends on test ordering. This does not.
    """
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger(logger_name)
    handler = _Collector(level=logging.WARNING)
    previous_level, previous_disabled = logger.level, logger.disabled
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    # The migration tests call alembic's `fileConfig`, which defaults to
    # disable_existing_loggers=True and leaves every already-created application
    # logger disabled for the rest of the process. Undo that here so this
    # assertion does not depend on which tests ran before it.
    logger.disabled = False
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.disabled = previous_disabled


@pytest.fixture
def session(_isolated_db):
    import db.session as session_module

    with session_module._SessionLocal() as s:
        yield s


def _make_run(session, *, run_id: str, user_id: str, counts: dict | None = None, dry_run: bool = False):
    from db.models import ChannelAccount, TriageRun, User

    if session.get(User, user_id) is None:
        session.add(User(id=user_id, email=f"{user_id}@example.com", display_name=user_id))
        session.add(
            ChannelAccount(
                id=f"conn-{user_id}",
                user_id=user_id,
                channel="gmail",
                account_email=f"{user_id}@gmail.com",
                refresh_token_enc="ENC",
                scopes=["gmail.readonly"],
                status="connected",
                connected_at=datetime.now(timezone.utc),
            )
        )
    run = TriageRun(
        id=run_id,
        user_id=user_id,
        channel_account_id=f"conn-{user_id}",
        status="completed",
        dry_run=dry_run,
        items_total=0,
        items_decided=0,
        counts=counts or {},
        started_at=datetime.now(timezone.utc),
    )
    session.add(run)
    session.flush()
    return run


def _add_decisions(session, *, run_id: str, user_id: str, autonomy_state, status: str, n: int, tag: str):
    from db.models import Decision, Item

    for idx in range(n):
        item_id = f"item-{tag}-{idx}"
        session.add(
            Item(
                id=item_id,
                user_id=user_id,
                channel_account_id=f"conn-{user_id}",
                external_thread_id=item_id,
                external_message_ids=[item_id],
                subject="s",
                from_email="a@b.com",
                from_domain="b.com",
                message_count=1,
                internal_date=datetime.now(timezone.utc),
            )
        )
        session.add(
            Decision(
                id=f"dec-{tag}-{idx}",
                user_id=user_id,
                item_id=item_id,
                run_id=run_id,
                proposed_action="archive",
                confidence=0.85,
                reasoning="r",
                decided_by="llm",
                status=status,
                review_state="reviewed",
                autonomy_state=autonomy_state,
            )
        )
    session.flush()


class TestHealthyRun:
    """Every bucket non-empty, everything the agent claimed it would archive applied."""

    @pytest.fixture
    def ledger(self, session):
        _make_run(session, run_id="run-1", user_id="u1")
        _add_decisions(session, run_id="run-1", user_id="u1", autonomy_state="auto_act", status="applied", n=544, tag="ap")
        _add_decisions(session, run_id="run-1", user_id="u1", autonomy_state="needs_your_call", status="needs_your_call", n=213, tag="nyc")
        _add_decisions(session, run_id="run-1", user_id="u1", autonomy_state="category_keep", status="proposed", n=1314, tag="ck")
        _add_decisions(session, run_id="run-1", user_id="u1", autonomy_state="held_by_never_miss", status="proposed", n=34, tag="nm")
        _add_decisions(session, run_id="run-1", user_id="u1", autonomy_state="below_threshold", status="proposed", n=71, tag="bt")
        return remainder_ledger(session, run_id="run-1", user_id="u1")

    def test_the_arithmetic_invariant_holds(self, ledger):
        assert ledger["inbox_remaining"] == sum(ledger["remainder"].values()) + ledger["distance_to_zero"]

    def test_applied_plus_remaining_accounts_for_every_decision(self, ledger):
        assert ledger["applied"] + ledger["inbox_remaining"] == 544 + 213 + 1314 + 34 + 71

    def test_the_buckets_are_reported_verbatim(self, ledger):
        assert ledger["remainder"] == {
            "needs_your_call": 213,
            "category_keep": 1314,
            "held_by_never_miss": 34,
            "below_threshold": 71,
            "unclassified": 0,
        }

    def test_distance_to_zero_is_zero_and_the_run_reads_as_healthy(self, ledger):
        assert ledger["distance_to_zero"] == 0
        assert ledger["apply_ok"] is True
        assert ledger["apply_failed_reason"] is None
        assert ledger["failures"] == []

    def test_every_bucket_key_is_always_present(self, ledger):
        assert tuple(ledger["remainder"]) == REMAINDER_BUCKETS


class TestTheBrokenRun:
    """Run fbeed060: 615 archives the agent decided on, none of them applied."""

    @pytest.fixture
    def ledger(self, session):
        _make_run(
            session,
            run_id="run-broken",
            user_id="u1",
            counts={"apply": {"apply_failed_reason": "RefreshError: invalid_grant", "failures": [{"decision_id": "dec-x", "error": "boom"}]}},
        )
        _add_decisions(session, run_id="run-broken", user_id="u1", autonomy_state="auto_act", status="proposed", n=615, tag="stuck")
        return remainder_ledger(session, run_id="run-broken", user_id="u1")

    def test_distance_to_zero_counts_every_unapplied_auto_act(self, ledger):
        assert ledger["distance_to_zero"] == 615
        assert ledger["applied"] == 0

    def test_it_can_never_render_as_a_clean_success(self, ledger):
        assert ledger["apply_ok"] is False
        assert ledger["apply_failed_reason"] == "RefreshError: invalid_grant"
        assert ledger["failures"] == [{"decision_id": "dec-x", "error": "boom"}]

    def test_the_unapplied_archives_are_not_in_a_remainder_bucket(self, ledger):
        # They are not "kept" — they are the debt. Folding them into a bucket is
        # exactly how the defect stayed invisible.
        assert sum(ledger["remainder"].values()) == 0
        assert ledger["inbox_remaining"] == 615


class TestEdgeCases:
    def test_pre_phase_7_rows_land_in_unclassified_and_are_never_folded_away(self, session):
        _make_run(session, run_id="run-old", user_id="u1")
        _add_decisions(session, run_id="run-old", user_id="u1", autonomy_state=None, status="proposed", n=7, tag="old")
        _add_decisions(session, run_id="run-old", user_id="u1", autonomy_state="category_keep", status="proposed", n=2, tag="new")

        ledger = remainder_ledger(session, run_id="run-old", user_id="u1")

        assert ledger["remainder"]["unclassified"] == 7
        assert ledger["remainder"]["category_keep"] == 2
        assert ledger["inbox_remaining"] == sum(ledger["remainder"].values()) + ledger["distance_to_zero"] == 9

    def test_a_run_with_no_decisions_reports_zeroes_not_an_error(self, session):
        _make_run(session, run_id="run-empty", user_id="u1")

        ledger = remainder_ledger(session, run_id="run-empty", user_id="u1")

        assert ledger["inbox_remaining"] == 0
        assert ledger["applied"] == 0
        assert ledger["apply_ok"] is True
        assert ledger["remainder"] == dict.fromkeys(REMAINDER_BUCKETS, 0)

    def test_dry_run_is_reported_from_the_run_row(self, session):
        _make_run(session, run_id="run-dry", user_id="u1", dry_run=True)

        assert remainder_ledger(session, run_id="run-dry", user_id="u1")["dry_run"] is True

    def test_an_applied_row_is_counted_once_whatever_its_autonomy_state(self, session):
        _make_run(session, run_id="run-mixed", user_id="u1")
        _add_decisions(session, run_id="run-mixed", user_id="u1", autonomy_state="category_keep", status="applied", n=3, tag="odd")

        ledger = remainder_ledger(session, run_id="run-mixed", user_id="u1")

        assert ledger["applied"] == 3
        assert ledger["inbox_remaining"] == 0


class TestErrorPaths:
    def test_another_users_run_reports_nothing(self, session):
        _make_run(session, run_id="run-a", user_id="u1")
        _add_decisions(session, run_id="run-a", user_id="u1", autonomy_state="category_keep", status="proposed", n=4, tag="a")

        ledger = remainder_ledger(session, run_id="run-a", user_id="intruder")

        assert ledger["inbox_remaining"] == 0
        assert ledger["remainder"]["category_keep"] == 0
        assert ledger["dry_run"] is False  # the other user's run row is not read either

    def test_an_unknown_autonomy_state_is_reported_not_dropped(self, session):
        _make_run(session, run_id="run-weird", user_id="u1")
        _add_decisions(session, run_id="run-weird", user_id="u1", autonomy_state="quantum_superposition", status="proposed", n=5, tag="w")

        with capture_warnings("zero_inbox.remainder") as records:
            ledger = remainder_ledger(session, run_id="run-weird", user_id="u1")

        assert ledger["remainder"]["unclassified"] == 5
        assert ledger["inbox_remaining"] == 5
        assert any("unknown_autonomy_state" in r.getMessage() for r in records)

    def test_a_missing_run_row_does_not_raise(self, session):
        ledger = remainder_ledger(session, run_id="does-not-exist", user_id="u1")

        assert ledger["run_id"] == "does-not-exist"
        assert ledger["inbox_remaining"] == 0
        assert ledger["apply_failed_reason"] is None
