"""The fast per-commit integration gate: one real triage run over 25 tiered threads.

Real NVIDIA NIM (key from `.env`), real persistence — nothing stubbed. One LLM
batch, so the whole file runs in minutes. The full 220-thread gate lives in
test_triage_pipeline.py behind `-m slow`.

This file is also the live regression for the Phase-1 gate defects:
- tier 3 answers with structured output on attempt 1 (no reasoning burn-through,
  no `decided_by='error'` storm),
- the default taxonomy is auto-seeded for a user who never got it, so every
  non-error decision carries a real `category_id`,
- run-row progress (`items_total` / `items_decided`) is written.
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy import select

from graph.runner import execute_triage

from tests.integration._threads_fixture import (
    ACCOUNT_ID,
    SMALL_TOTAL,
    USER_ID,
    build_threads_small,
    seed_user,
)

pytestmark = pytest.mark.integration

VALID_TIERS = {"rule", "sender_history", "llm", "llm_deep", "error", "reviewer"}
FREE_TIER_MINIMUM = 16  # 6 substack + 4 github + 3 bulk + 3 replied-to


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
    """One real 25-thread run, executed once and shared by every assertion."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import db.session as session_module
    from db.models import Base

    if "engine" not in _RUN:
        path = tmp_path_factory.mktemp("triage_small") / "run.db"
        engine = create_engine(f"sqlite:///{path}")
        Base.metadata.create_all(engine)
        _RUN["engine"] = engine
        _RUN["factory"] = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    monkeypatch.setattr(session_module, "_engine", _RUN["engine"])
    monkeypatch.setattr(session_module, "_SessionLocal", _RUN["factory"])

    if "state" not in _RUN:
        from db.session import create_db_session

        with create_db_session() as session:
            # No categories on purpose: the run itself must seed the six defaults.
            seed_user(session, with_categories=False)

        threads = build_threads_small()
        started = time.monotonic()
        _RUN["state"] = execute_triage(
            user_id=USER_ID, channel_account_id=ACCOUNT_ID, items=threads, dry_run=True
        )
        _RUN["elapsed_s"] = time.monotonic() - started
    return _RUN["state"]


def test_the_run_completes_and_every_thread_is_decided(triage_run):
    from db.models import Decision, Item
    from db.session import create_db_session

    assert triage_run["status"] == "completed"
    assert triage_run["counts"]["total"] == SMALL_TOTAL

    with create_db_session() as session:
        decisions = list(session.execute(select(Decision)).scalars())
        items = list(session.execute(select(Item)).scalars())
        assert len(items) == SMALL_TOTAL
        assert len(decisions) == SMALL_TOTAL
        assert len({d.item_id for d in decisions}) == SMALL_TOTAL
        assert all(d.decided_by in VALID_TIERS for d in decisions)
        assert all(d.reasoning.strip() for d in decisions)


def test_tier3_answers_structurally_no_error_degradation_storm(triage_run):
    """Regression for the reasoning-model burn-through: with response_format +
    thinking disabled, the batch classifies on the spot instead of degrading to
    decided_by='error' (143/200 observed pre-fix)."""
    by_tier = triage_run["counts"]["by_tier"]
    assert by_tier.get("error", 0) == 0, by_tier
    assert by_tier.get("llm", 0) + by_tier.get("llm_deep", 0) > 0, by_tier
    # "reviewer" only fires on candidates already resolved by tiers 1-2 (the
    # never-miss reviewer re-tags rule/sender_history decisions it flips) — it
    # doesn't represent new LLM cost, so it counts toward the cheap-resolution total.
    assert (
        by_tier.get("rule", 0) + by_tier.get("sender_history", 0) + by_tier.get("reviewer", 0)
        >= FREE_TIER_MINIMUM
    )


def test_the_llm_ran_batched_within_the_time_budget(triage_run):
    classify = [c for c in triage_run.get("llm_calls", []) if c["purpose"] == "classify"]
    assert classify, "tier 3 must really have called the model"
    assert all(c["tokens_in"] > 0 for c in classify)
    # A 25-thread run must be minutes, not the ~8-minute pre-fix pathology.
    assert _RUN["elapsed_s"] < 300, f"run took {_RUN['elapsed_s']:.0f}s"


def test_default_taxonomy_was_auto_seeded_and_category_ids_resolve(triage_run):
    """Regression: categories table had 0 rows, so every decisions.category_id was
    NULL and the UI showed category:null everywhere."""
    from db.models import Category, Decision
    from db.session import create_db_session
    from tools.rules import DEFAULT_CATEGORY_KEYS

    with create_db_session() as session:
        categories = list(
            session.execute(select(Category).where(Category.user_id == USER_ID)).scalars()
        )
        assert {c.key for c in categories} == set(DEFAULT_CATEGORY_KEYS)
        assert all(c.is_default for c in categories)

        for decision in session.execute(select(Decision)).scalars():
            if decision.decided_by != "error":
                assert decision.category_id is not None, decision.id


def test_run_row_progress_and_cost_are_recorded(triage_run):
    from db.models import TriageRun
    from db.session import create_db_session

    with create_db_session() as session:
        run = session.execute(select(TriageRun)).scalars().one()
        assert run.status == "completed"
        assert run.items_total == SMALL_TOTAL
        assert run.items_decided == SMALL_TOTAL
        assert sum(run.counts["by_tier"].values()) == SMALL_TOTAL
        assert run.tokens_in > 0 and run.tokens_out > 0
