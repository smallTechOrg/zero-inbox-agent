"""Seams: taxonomy → graph → llm → db, with the REAL LLM (keys from .env).

Runs the full triage pipeline through ``POST /api/runs`` against a synthetic
inbox (Gmail reads faked at the seam, writes sandboxed by the isolation guard,
LLM calls real). Each test fails when its seam is cut:

- graph → llm:   decisions carry real model output (category/confidence/reason)
- graph → db:    thread_decisions + llm_calls rows persisted
- taxonomy → graph: a rename between chunks changes the NEXT chunk's filing
- privacy:       the body sentinel never appears in any outgoing payload
- never-redo:    a second run re-decides nothing
"""

from __future__ import annotations

import pytest

from tests.fixtures.fake_gmail import install_inbox
from tests.fixtures.threads import BODY_SENTINEL, sample_threads
from tests.integration._helpers import run_to_completion

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture
def small_inbox(monkeypatch):
    threads = sample_threads(4)
    install_inbox(monkeypatch, threads)
    return threads


class TestRealTriageChunk:
    def test_full_chunk_decides_every_thread_with_real_llm(
        self, auth_client, seeded_user, small_inbox, db_session, llm_payload_capture, _require_llm_key
    ):
        user, categories = seeded_user
        run_id, run = run_to_completion(auth_client, chunk_limit=4)

        assert run["status"] == "completed", f"run did not complete: {run}"

        from db import models

        decisions = (
            db_session.query(models.ThreadDecision)
            .filter(models.ThreadDecision.user_id == user.id)
            .all()
        )
        decided_ids = {d.gmail_thread_id for d in decisions}
        assert decided_ids == {t["thread_id"] for t in small_inbox}, (
            f"every thread must get exactly one decision; got {decided_ids}"
        )
        valid_category_ids = {c.id for c in categories.values()}
        for d in decisions:
            assert d.category_id in valid_category_ids, f"unknown category on {d.gmail_thread_id}"
            assert 0.0 <= d.confidence <= 1.0
            assert d.reason and d.reason.strip(), "each decision carries a one-line reason"
            assert d.run_id == run_id
            if d.confidence < 0.7:
                assert d.needs_review, "confidence < 0.7 must set needs_review (spec)"

        # graph → llm seam: real calls were made and cost-accounted.
        llm_rows = db_session.query(models.LlmCall).filter(models.LlmCall.run_id == run_id).all()
        assert llm_rows, "no llm_calls rows — the graph → llm seam is cut"
        assert any(r.provider in ("nvidia", "gemini") for r in llm_rows)

        # The obvious newsletter should be filed to a label_and_archive category
        # — asserted loosely via mutations: at least one remove_inbox occurred
        # OR the newsletter decision is Newsletters. (Model-tolerant but seam-strict.)
        newsletter = next(d for d in decisions if d.gmail_thread_id == "test-thr-002")
        newsletter_cat = {c.id: n for n, c in categories.items()}[newsletter.category_id]
        assert newsletter_cat in ("Newsletters", "Notifications", "Needs review"), (
            f"a real model should not file Morning Brew as {newsletter_cat!r} — "
            "check the taxonomy → prompt seam"
        )

        # Privacy tripwire: payloads left the process, and none carried a body.
        assert llm_payload_capture, "no LLM payload captured — the capture seam is cut"
        for payload in llm_payload_capture:
            assert BODY_SENTINEL not in payload, (
                "EMAIL BODY LEAKED INTO AN LLM PAYLOAD — privacy boundary broken"
            )

    def test_run_totals_and_counts_are_written(self, auth_client, seeded_user, small_inbox, db_session, _require_llm_key):
        run_id, run = run_to_completion(auth_client, chunk_limit=4)
        assert run.get("threads_decided") == 4 or run.get("threads_decided") is None
        from db import models

        row = db_session.query(models.Run).filter(models.Run.id == run_id).one()
        assert row.status == "completed"
        assert row.threads_decided == 4
        assert row.counts_json, "per-category counts must be persisted on the run row"
        assert row.llm_calls and row.llm_calls > 0


class TestNeverRedo:
    def test_a_second_run_re_decides_nothing(self, auth_client, seeded_user, small_inbox, db_session, _require_llm_key):
        from db import models

        run_to_completion(auth_client, chunk_limit=4)
        first = db_session.query(models.ThreadDecision).count()

        run2_id, run2 = run_to_completion(auth_client, chunk_limit=4)
        db_session.expire_all()
        second = db_session.query(models.ThreadDecision).count()
        assert second == first, (
            f"never-redo broken: run 2 added {second - first} duplicate decisions"
        )
        assert run2["status"] == "completed", "an empty chunk still finalizes cleanly"


class TestTaxonomyAdaptation:
    def test_renaming_a_category_changes_the_next_chunk(
        self, auth_client, seeded_user, monkeypatch, db_session, llm_payload_capture, _require_llm_key
    ):
        """spec/capabilities/taxonomy-management.md success criterion #1."""
        user, categories = seeded_user
        install_inbox(monkeypatch, sample_threads(2))
        run_to_completion(auth_client, chunk_limit=2)

        # Rename between chunks; the next chunk must see the new name.
        finance = categories["Finance"]
        response = auth_client.patch(
            f"/api/taxonomy/{finance.id}", json={"name": "Money matters"}
        )
        assert response.json().get("ok") is True

        install_inbox(monkeypatch, sample_threads(4))  # adds 2 undecided threads
        llm_payload_capture.clear()
        run_to_completion(auth_client, chunk_limit=4)

        assert llm_payload_capture, "second chunk made no LLM call"
        joined = "\n".join(llm_payload_capture)
        assert "Money matters" in joined, (
            "taxonomy → graph seam is cut: the run classified against a stale "
            "taxonomy (renamed category absent from the prompt)"
        )
        assert '"Finance"' not in joined, "the old category name is still in the prompt"
