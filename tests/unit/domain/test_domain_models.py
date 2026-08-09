"""Domain model behaviour — the shapes the graph, API and adapters share."""

import pytest
from pydantic import ValidationError

from domain import (
    Cluster,
    Decision,
    DecidedBy,
    DecisionStatus,
    Item,
    ProposedAction,
    Rule,
    RuleAction,
    RuleMatcher,
    RuleStatus,
    RunStatus,
    TriageOutcome,
)


def _decision(**kw) -> Decision:
    base = dict(
        user_id="u1", item_id="i1", run_id="r1", decided_by=DecidedBy.LLM, confidence=0.8
    )
    return Decision(**{**base, **kw})


# --- happy path ---------------------------------------------------------------------


def test_item_normalizes_domain_and_flags_mailing_list():
    item = Item(
        user_id="u1",
        channel_account_id="ca1",
        external_thread_id="t1",
        from_email="Post@Substack.com",
        from_domain="@Substack.COM",
        list_id="<weekly.substack.com>",
    )
    assert item.from_domain == "substack.com"
    assert item.is_mailing_list is True
    assert Item.domain_of("a@Example.COM") == "example.com"


def test_triage_outcome_reports_tiers_costs_and_ratio():
    outcome = TriageOutcome(
        run_id="r1",
        status=RunStatus.COMPLETED,
        items_total=4,
        decisions=[
            _decision(item_id="i1", decided_by=DecidedBy.RULE),
            _decision(item_id="i2", decided_by=DecidedBy.SENDER_HISTORY),
            _decision(item_id="i3", decided_by=DecidedBy.LLM),
            _decision(
                item_id="i4",
                decided_by=DecidedBy.LLM,
                status=DecisionStatus.NEEDS_YOUR_CALL,
            ),
        ],
        clusters=[Cluster(user_id="u1", run_id="r1", label="Substack", item_count=4)],
    )
    assert outcome.items_decided == 4
    assert outcome.counts_by_tier() == {"rule": 1, "sender_history": 1, "llm": 2}
    assert outcome.needs_your_call == 1
    assert outcome.rules_vs_llm_ratio == pytest.approx(0.5)
    counts = outcome.counts()
    assert set(counts) == {"by_tier", "by_category", "needs_your_call"}


def test_rule_only_acts_unattended_when_automatic():
    rule = Rule(
        user_id="u1",
        name="Archive Substack",
        matcher=RuleMatcher(list_id="<weekly.substack.com>"),
        action=RuleAction(archive=True, set_category="newsletters"),
    )
    assert rule.acts_without_approval is False
    assert rule.action.proposed_action is ProposedAction.ARCHIVE
    assert rule.model_copy(update={"status": RuleStatus.AUTOMATIC}).acts_without_approval


# --- edge cases ---------------------------------------------------------------------


def test_empty_matcher_is_detected_so_it_can_never_match_everything():
    assert RuleMatcher().is_empty is True
    assert RuleMatcher(from_domain="x.com").is_empty is False


def test_empty_outcome_reports_zero_ratio_not_a_division_error():
    outcome = TriageOutcome(run_id="r1")
    assert outcome.rules_vs_llm_ratio == 0.0
    assert outcome.counts_by_tier() == {}
    assert outcome.items_decided == 0


def test_item_with_no_sender_domain_is_blank_not_an_error():
    item = Item(user_id="u1", channel_account_id="ca1", external_thread_id="t1")
    assert item.from_domain == ""
    assert item.is_mailing_list is False
    assert Item.domain_of("not-an-email") == ""


def test_confidence_floor_pushes_low_confidence_archive_to_needs_your_call():
    decision = _decision(proposed_action=ProposedAction.ARCHIVE, confidence=0.4)
    guarded = decision.enforce_floor(0.75)
    assert guarded.status is DecisionStatus.NEEDS_YOUR_CALL
    assert guarded.proposed_action is ProposedAction.KEEP


def test_confidence_floor_leaves_keep_and_confident_archive_untouched():
    keep = _decision(proposed_action=ProposedAction.KEEP, confidence=0.1)
    assert keep.enforce_floor(0.75).status is DecisionStatus.PROPOSED
    confident = _decision(proposed_action=ProposedAction.ARCHIVE, confidence=0.99)
    assert confident.enforce_floor(0.75).proposed_action is ProposedAction.ARCHIVE


# --- error paths --------------------------------------------------------------------


def test_decision_requires_a_deciding_tier():
    with pytest.raises(ValidationError):
        Decision(user_id="u1", item_id="i1", run_id="r1")


def test_confidence_outside_zero_to_one_is_rejected():
    for bad in (-0.1, 1.5):
        with pytest.raises(ValidationError):
            _decision(confidence=bad)


def test_unknown_action_or_tier_value_is_rejected():
    with pytest.raises(ValidationError):
        _decision(proposed_action="delete")
    with pytest.raises(ValidationError):
        _decision(decided_by="vibes")


def test_item_requires_a_tenant_and_a_thread_id():
    with pytest.raises(ValidationError):
        Item(channel_account_id="ca1", external_thread_id="t1")
    with pytest.raises(ValidationError):
        Item(user_id="u1", channel_account_id="ca1")
