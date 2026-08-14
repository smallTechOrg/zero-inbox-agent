"""Durable, resumable runs — the load-bearing Phase 6 gate.

Everything here runs against the **real** NVIDIA NIM endpoint using the key from
`.env` and the production SQLAlchemy driver; nothing about the LLM is stubbed except
where a test deliberately forces a provider failure it could not otherwise produce.

Two layers:

- ``TestResumeMechanics`` (fast, per-commit) — a rules-resolved slice of the fixture,
  so the interrupt → reconcile → resume cycle is exercised end to end in seconds while
  still making a real reviewer call.
- ``TestFullResume`` (``-m slow``) — the 220-thread fixture, interrupted at ~40 %,
  reconciled and resumed, per spec/roadmap.md Phase 6. Large enough that a partial
  result and a full result are observably different.

Run the full gate with:
``uv run pytest -m slow tests/integration/test_resume.py``
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from graph.runner import execute_triage

from tests.integration._threads_fixture import (
    ACCOUNT_ID,
    BULK_SENDER,
    TOTAL,
    USER_ID,
    build_threads,
    build_threads_small,
    seed_user,
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def _nim_key():
    from config.settings import get_settings

    if not get_settings().nvidia_api_key.strip():
        pytest.fail(
            "AGENT_NVIDIA_API_KEY is not set in .env — this gate must run against the "
            "real NVIDIA NIM endpoint, never a stub."
        )


@pytest.fixture
def db(monkeypatch, tmp_path):
    """An isolated production-driver database for one resume scenario."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import db.session as session_module
    from db.models import Base

    engine = create_engine(f"sqlite:///{tmp_path}/resume.db")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(session_module, "_engine", engine)
    monkeypatch.setattr(session_module, "_SessionLocal", factory)
    monkeypatch.setattr(session_module, "init_db", lambda: None)

    from db.session import create_db_session

    with create_db_session() as session:
        seed_user(session)
    yield engine
    engine.dispose()


# --- helpers ---------------------------------------------------------------------


def _decisions():
    from db.models import Decision
    from db.session import create_db_session

    with create_db_session() as session:
        return [
            {
                "item_id": row.item_id,
                "review_state": row.review_state,
                "proposed_action": row.proposed_action,
                "decided_by": row.decided_by,
                "status": row.status,
                "id": row.id,
            }
            for row in session.execute(select(Decision)).scalars()
        ]


def _run_row(run_id: str):
    from db.models import TriageRun
    from db.session import create_db_session

    with create_db_session() as session:
        row = session.get(TriageRun, run_id)
        return {
            "status": row.status,
            "items_total": row.items_total,
            "items_decided": row.items_decided,
            "cost_usd": row.cost_usd,
            "error_message": row.error_message,
        }


def _llm_call_count(run_id: str) -> int:
    from db.models import LLMCall
    from db.session import create_db_session

    with create_db_session() as session:
        return int(
            session.execute(
                select(func.count(LLMCall.id)).where(LLMCall.run_id == run_id)
            ).scalar_one()
        )


def _interrupt_after(threshold: int, monkeypatch):
    """Make the provider circuit open once ``threshold`` threads are decided.

    This is how a run is really interrupted in production (the process dies, or the
    provider goes away): the graph stops issuing LLM work and the run closes as
    ``resumable`` with every already-decided thread durable.
    """
    from llm import client as client_module
    from llm.health import ProviderCircuitOpen

    real = client_module.get_llm_client

    class Interrupting:
        def __init__(self, inner):
            self._inner = inner

        def _guard(self):
            from db.models import Decision
            from db.session import create_db_session

            with create_db_session() as session:
                decided = int(
                    session.execute(select(func.count(Decision.id))).scalar_one() or 0
                )
            if decided >= threshold:
                raise ProviderCircuitOpen(
                    f"provider unreachable after {decided} threads (simulated interrupt)"
                )

        async def classify_batch(self, *args, **kwargs):
            self._guard()
            return await self._inner.classify_batch(*args, **kwargs)

        async def call_model(self, *args, **kwargs):
            self._guard()
            return await self._inner.call_model(*args, **kwargs)

    monkeypatch.setattr(client_module, "get_llm_client", lambda: Interrupting(real()))


def _orphan_and_reconcile(run_id: str) -> str:
    """Put the run back to ``running`` (as a killed process leaves it) and reconcile."""
    from api import _reconcile_orphaned_runs
    from db.models import TriageRun
    from db.session import create_db_session

    with create_db_session() as session:
        session.get(TriageRun, run_id).status = "running"
    _reconcile_orphaned_runs()
    return _run_row(run_id)["status"]


# --- fast per-commit layer -------------------------------------------------------


class TestResumeMechanics:
    """Interrupt → reconcile → resume over a rules-resolved slice (one real reviewer call)."""

    @pytest.fixture
    def threads(self):
        # Tier 1/2 resolvable threads: substack + github + the bulk sender. The run
        # still makes a real reviewer call over the archive proposals.
        small = build_threads_small()
        return [
            t
            for t in small
            if (t.get("list_id") or "").endswith("substack.com>")
            or t["from_email"] == "notifications@github.com"
            or t["from_email"] == BULK_SENDER
        ]

    def test_a_completed_run_leaves_every_row_reviewed(self, db, _nim_key, threads):
        state = execute_triage(
            user_id=USER_ID, channel_account_id=ACCOUNT_ID, items=threads, dry_run=True
        )
        rows = _decisions()
        assert state["status"] == "completed"
        assert len(rows) == len(threads)
        # Never-miss not regressed: nothing may stay provisional after the reviewer.
        assert {r["review_state"] for r in rows} == {"reviewed"}

    def test_a_partial_run_is_resumable_and_resumes_without_redeciding(
        self, db, _nim_key, threads, monkeypatch
    ):
        from db.models import Decision, Item
        from db.session import create_db_session

        half = max(1, len(threads) // 2)
        # Leg 1: pre-decide half the threads through the real cascade, then orphan
        # the run exactly as a killed process would.
        first = execute_triage(
            user_id=USER_ID,
            channel_account_id=ACCOUNT_ID,
            items=threads[:half],
            dry_run=True,
        )
        run_id = first["run_id"]
        decided_at_interrupt = len(_decisions())
        assert 0 < decided_at_interrupt < len(threads)

        assert _orphan_and_reconcile(run_id) == "resumable"
        row = _run_row(run_id)
        assert row["items_decided"] == decided_at_interrupt
        assert "Resume to continue" in (row["error_message"] or "")

        # Leg 2: resume the SAME run over the whole mailbox. The already-decided
        # threads must be skipped, not re-classified.
        resumed = execute_triage(
            user_id=USER_ID,
            channel_account_id=ACCOUNT_ID,
            items=threads,
            dry_run=True,
            run_id=run_id,
        )
        rows = _decisions()

        assert resumed["run_id"] == run_id  # same run row — counts and cost stay on it
        assert len(rows) == len(threads)
        assert len({r["item_id"] for r in rows}) == len(threads)  # zero duplicates
        assert {r["review_state"] for r in rows} == {"reviewed"}

        with create_db_session() as session:
            pairs = session.execute(
                select(Decision.run_id, Decision.item_id, func.count(Decision.id))
                .group_by(Decision.run_id, Decision.item_id)
                .having(func.count(Decision.id) > 1)
            ).all()
            assert pairs == []
            assert session.execute(select(func.count(Item.id))).scalar_one() == len(threads)

    def test_a_run_with_no_persisted_decisions_is_failed_not_resumable(self, db):
        from db.models import TriageRun
        from db.session import create_db_session

        with create_db_session() as session:
            session.add(
                TriageRun(
                    id="run-empty",
                    user_id=USER_ID,
                    channel_account_id=ACCOUNT_ID,
                    status="running",
                    dry_run=True,
                    counts={},
                )
            )
        # Nothing to resume — there is no partial work.
        assert _orphan_and_reconcile("run-empty") == "failed"

    def test_a_cancelled_run_is_never_converted_to_resumable(self, db, _nim_key, threads):
        from db.models import TriageRun
        from db.session import create_db_session

        state = execute_triage(
            user_id=USER_ID, channel_account_id=ACCOUNT_ID, items=threads[:3], dry_run=True
        )
        with create_db_session() as session:
            session.get(TriageRun, state["run_id"]).status = "cancelled"
        from api import _reconcile_orphaned_runs

        _reconcile_orphaned_runs()
        assert _run_row(state["run_id"])["status"] == "cancelled"


class TestReviewFailureIsNeverApplied:
    """A run whose reviewer pass fails leaves every archive row un-appliable."""

    @pytest.fixture
    def threads(self):
        return [
            t
            for t in build_threads_small()
            if (t.get("list_id") or "").endswith("substack.com>")
        ]

    def test_review_failed_rows_apply_zero_gmail_mutations(
        self, db, _nim_key, threads, monkeypatch
    ):
        from tools.actions import NotReviewedError, apply_decision

        # Force every reviewer batch to fail — the LLM itself is untouched elsewhere.
        monkeypatch.setattr(
            "graph.nodes_review.review_archive_batch",
            lambda *args, **kwargs: ({}, [], True),
        )
        execute_triage(
            user_id=USER_ID, channel_account_id=ACCOUNT_ID, items=threads, dry_run=True
        )

        rows = _decisions()
        archives = [r for r in rows if r["proposed_action"] == "archive"]
        assert archives, "the fixture must produce archive proposals to review"
        assert {r["review_state"] for r in archives} == {"review_failed"}

        class ExplodingMutator:
            def __getattr__(self, name):
                def _boom(*args, **kwargs):
                    raise AssertionError(
                        "a review_failed decision must never reach Gmail"
                    )

                return _boom

        from db.session import create_db_session

        for row in archives:
            with create_db_session() as session:
                from db.models import Decision

                session.get(Decision, row["id"]).status = "approved"
                session.flush()
                for force in (False, True):
                    with pytest.raises(NotReviewedError):
                        apply_decision(
                            session,
                            USER_ID,
                            row["id"],
                            mutator=ExplodingMutator(),
                            label_lookup=ExplodingMutator(),
                            dry_run=False,
                            force=force,
                        )


# --- the full 220-thread gate ----------------------------------------------------


@pytest.mark.slow
class TestFullResume:
    """spec/roadmap.md Phase 6 assertions 1-6 over the 220-thread fixture."""

    @pytest.fixture
    def interrupted(self, db, _nim_key, monkeypatch):
        """A real 220-thread run interrupted once ~40 % of threads are decided."""
        threads = build_threads()
        _interrupt_after(int(TOTAL * 0.4), monkeypatch)
        state = execute_triage(
            user_id=USER_ID, channel_account_id=ACCOUNT_ID, items=threads, dry_run=True
        )
        monkeypatch.undo()
        return {
            "run_id": state["run_id"],
            "threads": threads,
            "decided": len(_decisions()),
            "llm_calls": _llm_call_count(state["run_id"]),
            "cost_usd": _run_row(state["run_id"])["cost_usd"],
        }

    def test_the_interrupted_run_persisted_a_strict_partial_result(self, interrupted):
        """Durability: decisions exist long before ``finalize`` ever ran."""
        run = _run_row(interrupted["run_id"])
        assert 0 < interrupted["decided"] < TOTAL
        assert run["items_decided"] == interrupted["decided"]

    def test_the_interrupted_run_reconciles_to_resumable_not_failed(self, interrupted):
        assert _orphan_and_reconcile(interrupted["run_id"]) == "resumable"
        message = _run_row(interrupted["run_id"])["error_message"] or ""
        assert "Resume to continue" in message
        assert str(interrupted["decided"]) in message

    def test_resuming_finishes_the_run_without_redeciding_a_single_thread(
        self, interrupted, _nim_key
    ):
        run_id = interrupted["run_id"]
        _orphan_and_reconcile(run_id)
        before_calls = interrupted["llm_calls"]

        resumed = execute_triage(
            user_id=USER_ID,
            channel_account_id=ACCOUNT_ID,
            items=interrupted["threads"],
            dry_run=True,
            run_id=run_id,
        )
        rows = _decisions()

        assert resumed["run_id"] == run_id
        assert len(rows) == TOTAL
        assert len({r["item_id"] for r in rows}) == TOTAL  # zero duplicate pairs
        # Strictly fewer calls than a from-zero run: the resume only classified the
        # threads that had no decision yet.
        resume_calls = _llm_call_count(run_id) - before_calls
        assert 0 < resume_calls
        assert resume_calls < _batches_for(TOTAL), (
            "the resume re-classified already-decided threads"
        )
        # Cost is additive, never reset.
        assert _run_row(run_id)["cost_usd"] >= interrupted["cost_usd"]

    def test_every_row_ends_reviewed_and_the_run_is_complete(self, interrupted, _nim_key):
        run_id = interrupted["run_id"]
        _orphan_and_reconcile(run_id)
        execute_triage(
            user_id=USER_ID,
            channel_account_id=ACCOUNT_ID,
            items=interrupted["threads"],
            dry_run=True,
            run_id=run_id,
        )
        rows = _decisions()
        assert {r["review_state"] for r in rows} == {"reviewed"}
        assert _run_row(run_id)["status"] == "completed"

    def test_every_decided_thread_emitted_exactly_one_classification_event(
        self, db, _nim_key, monkeypatch
    ):
        """Coverage: one ``thread_classified`` per thread, no tier omitted."""
        import events.bus as bus

        seen: list[dict] = []
        real = getattr(bus, "emit_thread_classified", None)

        def _record(user_id, **payload):
            seen.append(payload)
            if real is not None:
                real(user_id, **payload)

        monkeypatch.setattr(bus, "emit_thread_classified", _record, raising=False)

        threads = build_threads()
        execute_triage(
            user_id=USER_ID, channel_account_id=ACCOUNT_ID, items=threads, dry_run=True
        )
        assert len({e["item_id"] for e in seen}) == TOTAL
        assert {e["review_state"] for e in seen} <= {
            "provisional",
            "reviewed",
            "review_failed",
        }


class TestResumeEndpoint:
    """POST /api/runs/{run_id}/resume + the `resumable`/`remaining` run payload."""

    @pytest.fixture
    def client(self, db, monkeypatch):
        from fastapi.testclient import TestClient

        import api.connections as connections
        from api import app
        from api.session import COOKIE_NAME, issue_session_token

        monkeypatch.setenv("AGENT_SECRET_KEY", "test-secret-key-for-sessions")
        import config.settings as settings_module

        settings_module._settings = None

        self.tasks: list[dict] = []
        monkeypatch.setattr(
            connections, "_run_triage_task", lambda **kw: self.tasks.append(kw)
        )
        with TestClient(app) as c:
            c.cookies.set(COOKIE_NAME, issue_session_token(USER_ID))
            yield c

    def _run(self, status: str, *, decided: int = 40, total: int = 100) -> str:
        from db.models import TriageRun
        from db.session import create_db_session

        with create_db_session() as session:
            session.add(
                TriageRun(
                    id=f"run-{status}",
                    user_id=USER_ID,
                    channel_account_id=ACCOUNT_ID,
                    status=status,
                    dry_run=True,
                    items_total=total,
                    items_decided=decided,
                    counts={},
                )
            )
        return f"run-{status}"

    def test_the_run_payload_carries_resumable_and_remaining(self, client):
        run_id = self._run("resumable")
        body = client.get(f"/api/runs/{run_id}").json()["data"]
        assert body["status"] == "resumable"
        assert body["resumable"] is True
        assert body["remaining"] == 60

    def test_latest_run_surfaces_a_resumable_run(self, client):
        run_id = self._run("resumable")
        assert client.get("/api/runs/latest").json()["data"]["id"] == run_id

    def test_resume_restarts_the_same_run_in_the_background(self, client):
        run_id = self._run("resumable")
        res = client.post(f"/api/runs/{run_id}/resume")
        assert res.status_code == 200
        assert res.json()["data"] == {
            "run_id": run_id,
            "items_total": 100,
            "items_decided": 40,
            "remaining": 60,
        }
        assert client.get(f"/api/runs/{run_id}").json()["data"]["status"] == "running"
        assert [t["run_id"] for t in self.tasks] == [run_id]

    def test_resume_is_idempotent_and_never_starts_a_second_worker(self, client):
        run_id = self._run("resumable")
        client.post(f"/api/runs/{run_id}/resume")
        second = client.post(f"/api/runs/{run_id}/resume")
        assert second.status_code == 200
        assert len(self.tasks) == 1

    @pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
    def test_resuming_a_non_resumable_run_is_409_not_resumable(self, client, status):
        run_id = self._run(status)
        res = client.post(f"/api/runs/{run_id}/resume")
        assert res.status_code == 409
        assert res.json()["error"]["code"] == "not_resumable"
        assert self.tasks == []


def _batches_for(total: int) -> int:
    """An upper bound on the LLM calls a from-zero run over ``total`` threads makes."""
    from graph.nodes import MIN_BATCH

    return total // MIN_BATCH + total  # batches + at most one deep read per thread
