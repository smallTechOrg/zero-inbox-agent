"""Seam: interrupted run → resume. Data-driven resumability (spec/agent.md).

``thread_decisions`` is the source of truth: a resumed run skips decided
threads, and a decided-but-unapplied decision (decision row exists, no audit
row) is re-applied idempotently — zero duplicate decisions, zero duplicate
mutations. Built so no LLM call is needed: every inbox thread is already
decided, isolating the resume seam itself.
"""

from __future__ import annotations

import pytest

from tests.fixtures.fake_gmail import install_inbox
from tests.fixtures.threads import sample_threads
from tests.integration._helpers import run_to_completion

pytestmark = pytest.mark.integration


@pytest.fixture
def interrupted_state(db_session, seeded_user, monkeypatch):
    """An interrupted run: thread 1 decided+applied, thread 2 decided only."""
    from db import models

    user, categories = seeded_user
    threads = sample_threads(2)
    install_inbox(monkeypatch, threads)
    finance = categories["Finance"]

    run = models.Run(
        id="test-run-interrupted",
        user_id=user.id,
        status="interrupted",
        trigger="clean_chunk",
        chunk_limit=2,
        interrupt_reason="NVIDIA rate limit — will resume",
    )
    db_session.add(run)
    for i, thread in enumerate(threads):
        db_session.add(
            models.ThreadDecision(
                id=f"test-dec-resume-{i}",
                user_id=user.id,
                run_id=run.id,
                gmail_thread_id=thread["thread_id"],
                sender=thread["sender"],
                subject=thread["subject"],
                snippet=thread["snippet"],
                category_id=finance.id,
                confidence=0.9,
                reason="pre-crash decision",
                needs_review=False,
                source="llm",
                undone=False,
            )
        )
    # Thread 1 was applied before the crash; thread 2 was not (no audit row).
    db_session.add(
        models.Mutation(
            id="test-mut-resume-0",
            user_id=user.id,
            run_id=run.id,
            gmail_thread_id=threads[0]["thread_id"],
            action="add_label",
            label_name="ZI/Finance",
            reason="pre-crash apply",
        )
    )
    db_session.commit()
    return run, threads, finance


class TestResume:
    def test_resume_re_decides_nothing_and_re_applies_only_the_unapplied(
        self, auth_client, interrupted_state, db_session
    ):
        from db import models

        run, threads, finance = interrupted_state
        run_id, resumed = run_to_completion(auth_client, chunk_limit=2)

        db_session.expire_all()
        # Zero duplicate decisions: still exactly 2, and none re-decided.
        decisions = db_session.query(models.ThreadDecision).all()
        assert len(decisions) == 2, (
            f"resume created duplicate decisions: {[(d.gmail_thread_id, d.run_id) for d in decisions]}"
        )
        assert all(d.reason == "pre-crash decision" for d in decisions), (
            "resume RE-DECIDED an already-decided thread (never-redo broken)"
        )

        # Thread 1 not double-applied; thread 2's decision now applied.
        t0_mutations = (
            db_session.query(models.Mutation)
            .filter(models.Mutation.gmail_thread_id == threads[0]["thread_id"])
            .filter(models.Mutation.action == "add_label")
            .count()
        )
        assert t0_mutations == 1, "resume double-applied an already-applied mutation"
        t1_mutations = (
            db_session.query(models.Mutation)
            .filter(models.Mutation.gmail_thread_id == threads[1]["thread_id"])
            .count()
        )
        assert t1_mutations >= 1, (
            "resume did not apply the decided-but-unapplied decision "
            "(decision row without audit row must be re-applied idempotently)"
        )

    def test_the_command_returns_a_run_that_finalizes(self, auth_client, interrupted_state):
        run_id, resumed = run_to_completion(auth_client, chunk_limit=2)
        assert resumed["status"] in ("completed",), f"resumed run must finalize: {resumed}"
