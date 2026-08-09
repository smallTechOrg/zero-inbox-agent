"""Full cost-tiered triage over 220 real-shaped threads against the REAL NVIDIA NIM
endpoint, using the key from `.env`.

Nothing here is stubbed: tiers 1-2 run for real, the remainder is really classified by
the model in batches of 20-50, and everything is really persisted to the production
SQLAlchemy driver.
"""

from __future__ import annotations

import math

import pytest
from sqlalchemy import select

from graph.runner import execute_triage

from _threads_fixture import (
    ACCOUNT_ID,
    BULK_SENDER,
    REPLIED_SENDERS,
    TOTAL,
    USER_ID,
    build_threads,
    seed_user,
)

VALID_TIERS = {"rule", "sender_history", "llm", "llm_deep", "error"}
VALID_ACTIONS = {"keep", "archive", "digest"}
CONFIDENCE_FLOOR = 0.75


@pytest.fixture(scope="module")
def _nim_key():
    from config.settings import get_settings

    if not get_settings().nvidia_api_key.strip():
        pytest.fail(
            "AGENT_NVIDIA_API_KEY is not set in .env — this gate must run against the "
            "real NVIDIA NIM endpoint, never a stub."
        )


_RUN: dict = {}


@pytest.fixture
def triage_run(monkeypatch, tmp_path_factory, _nim_key):
    """One real 220-thread run, shared by every assertion in this module.

    The run really calls NVIDIA NIM, so it is executed exactly once; each test then
    re-points the session factory at that run's database and inspects it.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import db.session as session_module
    from db.models import Base

    if "engine" not in _RUN:
        path = tmp_path_factory.mktemp("triage_pipeline") / "run.db"
        engine = create_engine(f"sqlite:///{path}")
        Base.metadata.create_all(engine)
        _RUN["engine"] = engine
        _RUN["factory"] = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    monkeypatch.setattr(session_module, "_engine", _RUN["engine"])
    monkeypatch.setattr(session_module, "_SessionLocal", _RUN["factory"])

    if "state" not in _RUN:
        from db.session import create_db_session

        with create_db_session() as session:
            seed_user(session)

        threads = build_threads()
        assert len(threads) == TOTAL
        _RUN["state"] = execute_triage(
            user_id=USER_ID, channel_account_id=ACCOUNT_ID, items=threads, dry_run=True
        )
    return _RUN["state"]


def _decisions(session):
    from db.models import Decision

    return list(session.execute(select(Decision)).scalars())


class TestEveryThreadIsDecided:
    def test_all_220_threads_have_a_persisted_decision(self, triage_run):
        from db.models import Decision, Item
        from db.session import create_db_session

        assert triage_run["status"] == "completed"
        assert triage_run["counts"]["total"] == TOTAL

        with create_db_session() as session:
            decisions = _decisions(session)
            items = list(session.execute(select(Item)).scalars())
            assert len(items) == TOTAL
            assert len(decisions) == TOTAL
            assert len({d.item_id for d in decisions}) == TOTAL

    def test_every_decision_carries_category_confidence_reasoning_and_tier(self, triage_run):
        from db.session import create_db_session

        with create_db_session() as session:
            for decision in _decisions(session):
                assert decision.decided_by in VALID_TIERS
                assert decision.proposed_action in VALID_ACTIONS
                assert 0.0 <= decision.confidence <= 1.0
                assert decision.reasoning.strip()
                assert decision.status in {"proposed", "needs_your_call"}
                if decision.decided_by != "error":
                    assert decision.category_id is not None

    def test_run_row_reports_per_tier_counts_and_cost(self, triage_run):
        from db.models import TriageRun
        from db.session import create_db_session

        with create_db_session() as session:
            run = session.execute(select(TriageRun)).scalars().one()
            assert run.status == "completed"
            assert run.items_total == TOTAL
            assert run.items_decided == TOTAL
            assert sum(run.counts["by_tier"].values()) == TOTAL
            assert run.tokens_in > 0 and run.tokens_out > 0


class TestCostTiering:
    def test_the_majority_is_resolved_by_the_free_tiers(self, triage_run):
        by_tier = triage_run["counts"]["by_tier"]
        free = by_tier.get("rule", 0) + by_tier.get("sender_history", 0)
        assert free > TOTAL / 2, by_tier
        assert by_tier.get("rule", 0) >= 96  # 62 Substack + 34 GitHub
        assert by_tier.get("sender_history", 0) >= 42  # 24 replied-to + 18 bulk

    def test_the_llm_is_batched_not_called_per_thread(self, triage_run):
        by_tier = triage_run["counts"]["by_tier"]
        remaining = by_tier.get("llm", 0) + by_tier.get("llm_deep", 0) + by_tier.get("error", 0)
        assert remaining > 0, "the fixture must leave work for the LLM tier"
        classify_calls = [
            c for c in triage_run.get("llm_calls", []) if c["purpose"] == "classify"
        ]
        assert classify_calls, "tier 3 must have really called the model"
        assert len(classify_calls) <= math.ceil(remaining / 20)
        assert all(20 <= c["items_in_batch"] <= 50 for c in classify_calls)

    def test_llm_call_rows_are_persisted_for_the_audit_trail(self, triage_run):
        from db.models import LLMCall
        from db.session import create_db_session

        with create_db_session() as session:
            calls = list(session.execute(select(LLMCall)).scalars())
            assert calls
            assert all(c.model for c in calls)
            assert all(c.tokens_in > 0 for c in calls)
            assert all(c.purpose in {"classify", "deep_read"} for c in calls)


class TestNeverMissInvariants:
    def test_nothing_below_the_confidence_floor_proposes_archive(self, triage_run):
        from db.session import create_db_session

        with create_db_session() as session:
            for decision in _decisions(session):
                if decision.confidence < CONFIDENCE_FLOOR:
                    assert decision.proposed_action == "keep"
                    assert decision.status == "needs_your_call"

    def test_no_thread_from_an_ever_replied_sender_is_proposed_for_archive(self, triage_run):
        from db.models import Decision, Item
        from db.session import create_db_session

        with create_db_session() as session:
            rows = session.execute(
                select(Item.from_email, Decision.proposed_action).join(
                    Decision, Decision.item_id == Item.id
                )
            ).all()
            replied = [action for email, action in rows if email in REPLIED_SENDERS]
            assert len(replied) == 24
            assert set(replied) == {"keep"}

    def test_the_bulk_sender_the_user_always_archives_is_proposed_for_archive(
        self, triage_run
    ):
        from db.models import Decision, Item
        from db.session import create_db_session

        with create_db_session() as session:
            rows = session.execute(
                select(Decision.proposed_action, Decision.decided_by)
                .join(Item, Decision.item_id == Item.id)
                .where(Item.from_email == BULK_SENDER)
            ).all()
            assert len(rows) == 18
            assert {(a, t) for a, t in rows} == {("archive", "sender_history")}

    def test_time_sensitive_threads_are_kept_visible(self, triage_run):
        from db.session import create_db_session

        with create_db_session() as session:
            flagged = [d for d in _decisions(session) if d.time_sensitive]
            assert all(d.proposed_action == "keep" for d in flagged)


class TestClustering:
    def test_cluster_item_counts_sum_to_220(self, triage_run):
        from db.models import Cluster
        from db.session import create_db_session

        with create_db_session() as session:
            clusters = list(session.execute(select(Cluster)).scalars())
            assert sum(c.item_count for c in clusters) == TOTAL

    def test_220_threads_collapse_to_at_most_40_decisions(self, triage_run):
        from db.models import Cluster
        from db.session import create_db_session

        with create_db_session() as session:
            clusters = list(session.execute(select(Cluster)).scalars())
            assert 0 < len(clusters) <= 40

    def test_every_decision_belongs_to_exactly_one_cluster(self, triage_run):
        from db.session import create_db_session

        with create_db_session() as session:
            decisions = _decisions(session)
            assert all(d.cluster_id is not None for d in decisions)

    def test_no_cluster_mixes_proposed_actions(self, triage_run):
        from db.models import Cluster
        from db.session import create_db_session

        with create_db_session() as session:
            by_cluster: dict[str, set[str]] = {}
            for decision in _decisions(session):
                by_cluster.setdefault(decision.cluster_id, set()).add(decision.proposed_action)
            for cluster in session.execute(select(Cluster)).scalars():
                actions = by_cluster.get(cluster.id, set())
                assert len(actions) == 1
                assert cluster.suggested_action in actions

    def test_a_large_newsletter_cluster_exists(self, triage_run):
        from db.models import Cluster
        from db.session import create_db_session

        with create_db_session() as session:
            clusters = list(session.execute(select(Cluster)).scalars())
            biggest = max(clusters, key=lambda c: c.item_count)
            assert biggest.item_count >= 20
            assert biggest.min_confidence <= biggest.avg_confidence


class TestResume:
    def test_a_resumed_run_produces_no_duplicate_decisions(self, triage_run):
        from db.session import create_db_session

        run_id = triage_run["run_id"]
        before = len(_decisions_in_new_session())

        execute_triage(
            user_id=USER_ID,
            channel_account_id=ACCOUNT_ID,
            items=build_threads(),
            run_id=run_id,
            dry_run=True,
        )

        with create_db_session() as session:
            after = _decisions(session)
            assert len(after) == before == TOTAL
            assert len({d.item_id for d in after}) == TOTAL


def _decisions_in_new_session():
    from db.session import create_db_session

    with create_db_session() as session:
        return _decisions(session)
