"""triage-graph slice — integration against the REAL LLM (keys from .env).

Gmail/DB/event effects are injected fakes (the parallel wave-1 slices own those
surfaces; the seams integrate at the phase gate). The classification path —
prompt build, batched call, strict-JSON parsing, confidence handling — is real.
"""

from __future__ import annotations

import json

import pytest

from graph.runner import _classify, build_instructions
from graph.state import ClassifierView, TaxonomyEntry, view_from_mapping

from tests.unit.test_graph_nodes import make_fake_deps, run

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

SENTINEL = "XKCD-BODY-SENTINEL-9271"

TAXONOMY = [
    TaxonomyEntry(id="c-fin", name="Finance",
                  description="Invoices, receipts, bank statements, payment and tax notices."),
    TaxonomyEntry(id="c-news", name="Newsletters",
                  description="Bulk newsletters and digests with an unsubscribe option.",
                  rule="label_and_archive"),
    TaxonomyEntry(id="c-notif", name="Notifications",
                  description="Automated service alerts: sign-ins, CI results, shipping updates.",
                  rule="label_and_archive"),
    TaxonomyEntry(id="c-pers", name="Personal",
                  description="Real people writing to the user personally."),
    TaxonomyEntry(id="c-rev", name="Needs review",
                  description="Anything uncertain.", is_needs_review=True),
]

VIEWS = [
    ClassifierView(
        thread_id="t-invoice",
        sender_address="billing@acme-corp.com", sender_name="ACME Billing",
        subject="Invoice #4821 for March — payment due April 15",
        snippet="Your invoice for $1,240.00 is attached. Please remit payment by",
        gmail_category="updates",
    ),
    ClassifierView(
        thread_id="t-newsletter",
        sender_address="newsletter@techdigest.io", sender_name="Tech Digest",
        subject="This week in AI: 10 stories you missed",
        snippet="Welcome to this week's roundup of the biggest stories in",
        has_list_unsubscribe=True, gmail_category="promotions",
    ),
    ClassifierView(
        thread_id="t-notification",
        sender_address="noreply@github.com", sender_name="GitHub",
        subject="[repo] Build failed: main #1287",
        snippet="The build for commit 3fa2c1 failed during the test step",
        has_list_unsubscribe=True, gmail_category="updates",
    ),
    ClassifierView(
        thread_id="t-personal",
        sender_address="maria.lopez@gmail.com", sender_name="Maria Lopez",
        subject="Re: dinner on Friday?",
        snippet="Sounds great! Shall we say 7pm at the usual place? I was",
        thread_message_count=4, has_user_replied=True, gmail_category="personal",
    ),
]

EXPECTED = {
    "t-invoice": "Finance",
    "t-newsletter": "Newsletters",
    "t-notification": "Notifications",
    "t-personal": "Personal",
}


class TestRealClassification:
    def test_happy_path_full_graph_run_classifies_obvious_threads(self, _require_llm_key):
        """Real LLM through the whole compiled graph with injected Gmail/DB fakes."""
        world = make_fake_deps(threads=list(VIEWS), taxonomy=TAXONOMY,
                               classify_fn=lambda v, t: _classify(v, t))
        summary = run(world)

        assert summary["status"] == "completed", summary["error"]
        assert summary["threads_decided"] == 4
        decided = {d.thread_id: d for d in world.saved}
        for thread_id, expected_category in EXPECTED.items():
            d = decided[thread_id]
            assert 0.0 <= d.confidence <= 1.0
            assert d.reason and "classifier output invalid" not in d.reason
            # unmistakable fixtures: the real model should place them correctly
            assert d.category_name == expected_category, (
                f"{thread_id}: got {d.category_name!r} ({d.reason!r})"
            )
        # real tokens were consumed and accounted
        assert summary["cost"]["calls"] >= 1
        assert summary["cost"]["tokens_in"] > 0
        assert summary["cost"]["tokens_out"] > 0
        # the archive rule was applied off real decisions
        muts = {(m["thread_id"], m["action"]) for m in world.mutations}
        assert ("t-newsletter", "remove_inbox") in muts
        assert ("t-invoice", "remove_inbox") not in muts

    def test_edge_ambiguous_thread_yields_valid_taxonomy_category(self, _require_llm_key):
        ambiguous = [ClassifierView(
            thread_id="t-ambiguous",
            sender_address="info@example.org", subject="Quick update",
            snippet="Just a quick note about the thing we discussed",
        )]
        outcome = _classify(ambiguous, TAXONOMY)
        names = {t.name for t in TAXONOMY}
        assert len(outcome.results) == 1
        result = outcome.results[0]
        assert result["category"] in names  # enum-constrained — never invented
        assert 0.0 <= float(result["confidence"]) <= 1.0

    def test_prompt_payload_contains_no_body_even_with_leaky_upstream(self, _require_llm_key):
        """The serialized instruction+item payload for a REAL call carries no body."""
        leaky = view_from_mapping({
            "thread_id": "t-leak", "subject": "Receipt for your purchase",
            "sender_address": "store@shop.com", "snippet": "Thanks for your order",
            "body": SENTINEL, "body_html": SENTINEL,
        })
        serialized = json.dumps([leaky.to_prompt_dict()]) + build_instructions(TAXONOMY)
        assert SENTINEL not in serialized
        outcome = _classify([leaky], TAXONOMY)
        assert outcome.results, "real call returned no results"

    def test_error_path_provider_failure_interrupts_run_humanely(self):
        """A hard provider failure surfaces as an interrupted run, no traceback."""

        def exploding(*a, **k):
            raise RuntimeError("HTTP 429 Too Many Requests")

        world = make_fake_deps(threads=list(VIEWS), taxonomy=TAXONOMY,
                               classify_fn=exploding)
        summary = run(world)
        assert summary["status"] == "interrupted"
        assert "rate-limited" in summary["error"]
        assert len(world.events_of("run_interrupted")) == 1
