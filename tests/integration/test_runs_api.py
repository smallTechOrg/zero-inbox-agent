"""Integration — runs-undo-api slice: trigger → feed → run card → undo, end to end.

The triage graph itself is a parallel slice; here its *contracts* are honoured
by a stand-in runner that does exactly what spec/agent.md requires of the real
one: writes ``thread_decisions``, mutates through the audited Gmail choke
point (audit row FIRST), persists every feed event via ``events.store``, and
finalises run totals. Gmail writes are simulated by the test-isolation guard;
everything else (DB, envelope, SSE, undo inversion) is real.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from tests.conftest import seed_user
from tests.unit.test_runs_api import ALICE, make_client

THREADS = [
    ("test-thread-a", "news@example.com", "Weekly digest", "Newsletters"),
    ("test-thread-b", "bank@example.com", "Your statement", "Finance"),
]


@pytest.fixture
def alice(db_session, monkeypatch):
    monkeypatch.setenv("AGENT_GMAIL_WRITE_DISABLED", "1")
    user, categories = seed_user(db_session, user_id=ALICE, email="alice@example.com")
    categories["Newsletters"].gmail_label_id = "test-label-newsletters"
    categories["Finance"].gmail_label_id = "test-label-finance"
    db_session.commit()
    return user, categories


def _contract_runner(*, run_id: str, user_id: str) -> None:
    """A spec/agent.md-shaped run: decide, audit-then-mutate, emit, finalise."""
    from channels.gmail.mutations import GmailMutator
    from db.models import Category, Mutation, Run, ThreadDecision
    from db.session import create_db_session
    from events.store import record_run_event

    with create_db_session() as session:
        run = session.get(Run, run_id)
        categories = {
            c.name: c for c in session.query(Category).filter_by(user_id=user_id)
        }
        record_run_event(
            session, user_id=user_id, run_id=run_id, type="chunk_loaded",
            sentence=f"Loaded {len(THREADS)} inbox threads (newest first).",
        )

        def audit_writer(record: dict) -> None:
            label_id = record.get("label_id")
            label_name = None
            if record["op"] == "add_label":
                label_name = next(
                    n for n, c in categories.items() if c.gmail_label_id == label_id
                )
            session.add(
                Mutation(
                    user_id=user_id,
                    run_id=run_id,
                    gmail_thread_id=record["gmail_thread_id"],
                    action=record["op"],
                    label_name=label_name,
                    reason=record["reason"],
                )
            )
            session.flush()

        mutator = GmailMutator(None, audit_writer=audit_writer)

        for thread_id, sender, subject, category_name in THREADS:
            category = categories[category_name]
            # Never-redo contract: gmail_thread_id is unique per user; an
            # UNDONE thread is re-decided by UPDATING its row (spec/data.md).
            existing = (
                session.query(ThreadDecision)
                .filter_by(user_id=user_id, gmail_thread_id=thread_id)
                .one_or_none()
            )
            if existing is not None:
                assert existing.undone, "a live decision must never be redone"
                existing.run_id = run_id
                existing.undone = False
                existing.category_id = category.id
            else:
                session.add(
                    ThreadDecision(
                        user_id=user_id, run_id=run_id, gmail_thread_id=thread_id,
                        sender=sender, subject=subject, category_id=category.id,
                        confidence=0.92, reason=f"clearly {category_name}",
                    )
                )
            record_run_event(
                session, user_id=user_id, run_id=run_id, type="decision",
                sentence=f"Filed '{subject}' → {category_name}.",
                detail={"confidence": 0.92, "reasoning": f"clearly {category_name}"},
            )
            result = mutator.apply(
                "add_label", thread_id,
                label_id=category.gmail_label_id, reason=f"filed to {category_name}",
            )
            assert result["simulated"] is True  # the guard held
            record_run_event(
                session, user_id=user_id, run_id=run_id, type="action",
                sentence=f"Labelled '{subject}' with {category_name}.",
            )
            if category.rule == "label_and_archive":
                mutator.apply("remove_inbox", thread_id, reason="rule: archive")
                record_run_event(
                    session, user_id=user_id, run_id=run_id, type="action",
                    sentence=f"Archived '{subject}'.",
                )

        run.status = "completed"
        run.threads_decided = len(THREADS)
        run.llm_calls = 1
        run.tokens_in, run.tokens_out, run.est_cost_usd = 700, 90, 0.0009
        run.finished_at = datetime.now(timezone.utc)
        record_run_event(
            session, user_id=user_id, run_id=run_id, type="run_finished",
            sentence="Run finished — 2 threads cleaned.",
        )


def test_full_run_feed_card_and_undo_journey(alice, db_session, monkeypatch):
    import api.runs as runs_module
    from channels.gmail.mutations import GmailMutator

    monkeypatch.setattr(runs_module, "_start_run_task", _contract_runner)
    monkeypatch.setattr(
        runs_module, "gmail_mutator_for_user", lambda session, user_id: GmailMutator(None)
    )

    with make_client(ALICE) as client:
        # 1) Manual trigger — the ONLY way a run starts.
        started = client.post("/api/runs", json={"chunk_limit": 50})
        assert started.status_code == 200, started.text
        run_id = started.json()["data"]["run_id"]

        # 2) The run card: completed, per-category counts, cost, undoable.
        card = client.get(f"/api/runs/{run_id}").json()["data"]
        assert card["status"] == "completed"
        assert card["counts"] == {"Newsletters": 1, "Finance": 1}
        assert card["threads_decided"] == 2
        assert card["cost"]["llm_calls"] == 1
        assert card["undo"]["undoable"] is True
        assert len(card["decisions"]) == 2

        # 3) Every decision and every mutation has exactly one feed event,
        #    and the feed replays completely (reload-mid-run guarantee).
        from db.models import Mutation

        mutation_count = db_session.query(Mutation).filter_by(run_id=run_id).count()
        assert mutation_count == 3  # 2 labels + 1 archive (Newsletters rule)

        frames = []
        with client.stream("GET", f"/api/runs/{run_id}/events") as resp:
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    frames.append(json.loads(line[6:]))
                    if frames[-1]["type"] == "run_finished":
                        break
        types = [f["type"] for f in frames]
        assert types.count("decision") == 2
        assert types.count("action") == mutation_count
        assert types[0] == "chunk_loaded" and types[-1] == "run_finished"
        assert [f["seq"] for f in frames] == sorted(f["seq"] for f in frames)

        # 4) ?after_seq resumes exactly where the client left off.
        cut = frames[2]["seq"]
        tail = []
        with client.stream("GET", f"/api/runs/{run_id}/events?after_seq={cut}") as resp:
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    tail.append(json.loads(line[6:]))
                    if tail[-1]["type"] == "run_finished":
                        break
        assert [f["seq"] for f in tail] == [f["seq"] for f in frames[3:]]

        # 5) Whole-run undo restores everything, newest first, audited.
        undone = client.post(f"/api/runs/{run_id}/undo").json()["data"]
        assert undone["status"] == "undone"
        assert undone["reversed"] == mutation_count
        assert undone["errors"] == []

        db_session.expire_all()
        from db.models import Run, RunEvent, ThreadDecision

        assert db_session.get(Run, run_id).status == "undone"
        assert all(
            m.undone_at is not None
            for m in db_session.query(Mutation).filter_by(run_id=run_id)
        )
        assert all(
            d.undone for d in db_session.query(ThreadDecision).filter_by(run_id=run_id)
        )
        undo_types = [
            e.type
            for e in db_session.query(RunEvent)
            .filter_by(run_id=run_id)
            .order_by(RunEvent.seq)
            .all()
            if e.type.startswith("undo_")
        ]
        assert undo_types[0] == "undo_started"
        assert undo_types[-1] == "undo_finished"
        assert undo_types.count("undo_action") == mutation_count

        # 6) Undone threads are re-decidable: a new trigger starts a NEW run
        #    (the undone one no longer blocks or resumes).
        second = client.post("/api/runs", json={})
        assert second.status_code == 200
        assert second.json()["data"]["run_id"] != run_id
