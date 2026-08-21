"""Slice db-and-domain — domain types, incl. the privacy boundary on ClassifierView."""

import pytest

from domain import (
    CLASSIFIER_VIEW_FIELDS,
    MUTATION_INVERSE,
    SNIPPET_MAX_CHARS,
    ClassifierView,
    CostTotals,
    Decision,
    MutationAction,
)


def _view(**overrides) -> ClassifierView:
    base = dict(
        gmail_thread_id="t-1",
        sender_address="news@example.test",
        sender_name="Example News",
        subject="Weekly digest",
        list_unsubscribe_present=True,
        reply_to=None,
        category_tab="promotions",
        thread_message_count=1,
        has_user_replied=False,
        snippet="This week in examples...",
    )
    base.update(overrides)
    return ClassifierView(**base)


class TestPrivacyBoundary:
    def test_field_list_is_exactly_the_allowed_set(self):
        """spec/architecture.md: only these fields may ever reach a prompt."""
        assert CLASSIFIER_VIEW_FIELDS == (
            "gmail_thread_id",
            "sender_address",
            "sender_name",
            "subject",
            "list_unsubscribe_present",
            "reply_to",
            "category_tab",
            "thread_message_count",
            "has_user_replied",
            "snippet",
        )

    def test_body_is_unrepresentable(self):
        with pytest.raises(TypeError):
            _view(body="full email body")  # no such field
        view = _view()
        with pytest.raises((AttributeError, TypeError)):
            view.body = "smuggled"  # frozen + slots: no attribute can be attached

    def test_snippet_is_hard_truncated(self):
        view = _view(snippet="x" * 5000)
        assert len(view.snippet) == SNIPPET_MAX_CHARS


class TestDecisionAndCosts:
    def test_decision_defaults_to_llm_source(self):
        d = Decision(
            gmail_thread_id="t-1", category_id="c-1", confidence=0.9,
            reason="clear newsletter", needs_review=False,
        )
        assert d.source == "llm"

    def test_cost_totals_accumulate(self):
        c = CostTotals()
        c.add_call(tokens_in=100, tokens_out=20, est_cost_usd=0.001)
        c.add_call(tokens_in=50, tokens_out=10, est_cost_usd=0.002, was_fallback=True)
        assert (c.llm_calls, c.tokens_in, c.tokens_out, c.fallback_events) == (2, 150, 30, 1)
        assert c.est_cost_usd == pytest.approx(0.003)


class TestMutationInverses:
    def test_pairs_are_involutions(self):
        for action, inverse in MUTATION_INVERSE.items():
            assert MUTATION_INVERSE[inverse] == action
        assert MUTATION_INVERSE[MutationAction.ADD_LABEL] == MutationAction.REMOVE_LABEL
        assert MUTATION_INVERSE[MutationAction.REMOVE_INBOX] == MutationAction.RESTORE_INBOX
