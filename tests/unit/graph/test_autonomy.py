"""The autonomy policy's pure core — threshold resolution and the fixed precedence.

spec/capabilities/drive-to-inbox-zero.md rules A1-A5 and B, and the
``mark_autonomy_state`` precedence in spec/agent.md § Nodes.

These are pure functions: no DB, no session, no LLM. If any of them ever needs
one, the policy has leaked out of its box.
"""

from __future__ import annotations

import pytest

from graph.autonomy import (
    AUTONOMY_STATES,
    DEFAULT_AUTO_ACT_THRESHOLD,
    classify_autonomy_state,
    effective_threshold,
    refresh_cluster_actions,
)

SETTINGS = {"auto_act_threshold": 0.80, "confidence_floor": 0.75}

NEWSLETTERS = {"key": "newsletters", "name": "Newsletters", "default_action": "archive",
               "auto_act_threshold": None}
OUTREACH = {"key": "outreach", "name": "Outreach", "default_action": "archive",
            "auto_act_threshold": 0.85}
PEOPLE = {"key": "people", "name": "People", "default_action": "keep",
          "auto_act_threshold": None}
URGENT = {"key": "urgent", "name": "Urgent", "default_action": "keep",
          "auto_act_threshold": None}


def _decision(**overrides) -> dict:
    base = {
        "item_id": "i1",
        "category": "newsletters",
        "proposed_action": "archive",
        "confidence": 0.88,
        "reasoning": "bulk newsletter",
        "decided_by": "llm",
        "rule_id": None,
        "time_sensitive": False,
        "unsure": False,
        "status": "proposed",
    }
    base.update(overrides)
    return base


# --- Rule A1-A3: threshold resolution ---------------------------------------


def test_pinned_default_is_the_calibrated_080():
    assert DEFAULT_AUTO_ACT_THRESHOLD == 0.80


def test_null_category_threshold_inherits_the_global_bar():
    assert effective_threshold(NEWSLETTERS, SETTINGS) == pytest.approx(0.80)


def test_category_override_wins_over_the_global_bar():
    assert effective_threshold(OUTREACH, SETTINGS) == pytest.approx(0.85)


def test_no_category_resolves_to_the_settings_only_bar():
    assert effective_threshold(None, SETTINGS) == pytest.approx(0.80)


def test_confidence_floor_is_a_hard_lower_bound_on_the_global_bar():
    # A user who drags the autonomy slider under the floor does not get to act
    # below the never-miss floor — the threshold may only raise the bar (A3).
    settings = {"auto_act_threshold": 0.10, "confidence_floor": 0.75}
    assert effective_threshold(NEWSLETTERS, settings) == pytest.approx(0.75)


def test_confidence_floor_is_a_hard_lower_bound_on_a_category_override():
    settings = {"auto_act_threshold": 0.90, "confidence_floor": 0.75}
    reckless = {**NEWSLETTERS, "auto_act_threshold": 0.05}
    assert effective_threshold(reckless, settings) == pytest.approx(0.75)


def test_effective_threshold_never_raises_on_junk_and_falls_back_to_defaults():
    assert effective_threshold({"auto_act_threshold": "banana"}, {}) == pytest.approx(0.80)
    assert effective_threshold(None, None) == pytest.approx(0.80)
    assert effective_threshold({}, {"confidence_floor": None}) == pytest.approx(0.80)


# --- Rule B: the calibration replayed against the measured distribution -------

# Run fbeed060's 615 archive proposals, exactly as measured:
#   >= 0.95 -> 0 | 0.90-0.94 -> 22 | 0.80-0.89 -> 522 | 0.75-0.79 -> 71
FBEED060_BANDS = [(0.97, 0), (0.92, 22), (0.85, 522), (0.77, 71)]


def _fbeed060_archive_decisions() -> list[dict]:
    rows = []
    for confidence, count in FBEED060_BANDS:
        for n in range(count):
            rows.append(
                _decision(item_id=f"i-{confidence}-{n}", confidence=confidence)
            )
    return rows


def test_the_measured_distribution_yields_544_auto_act_at_the_chosen_080_bar():
    decisions = _fbeed060_archive_decisions()
    assert len(decisions) == 615

    states = [
        classify_autonomy_state(d, NEWSLETTERS, SETTINGS) for d in decisions
    ]
    assert states.count("auto_act") == 544
    assert states.count("below_threshold") == 71


def test_the_same_distribution_at_095_yields_zero_auto_act_reproducing_the_defect():
    # This is the production behaviour before Phase 7: a bar above the model's
    # entire achievable range, so the inbox never moved.
    settings = {"auto_act_threshold": 0.95, "confidence_floor": 0.75}
    states = [
        classify_autonomy_state(d, NEWSLETTERS, settings)
        for d in _fbeed060_archive_decisions()
    ]
    assert states.count("auto_act") == 0
    assert states.count("below_threshold") == 615


# --- the fixed precedence of classify_autonomy_state -------------------------


def test_every_returned_state_is_one_of_the_five_pinned_values():
    assert set(AUTONOMY_STATES) == {
        "auto_act",
        "below_threshold",
        "held_by_never_miss",
        "category_keep",
        "needs_your_call",
    }


def test_needs_your_call_outranks_everything_else():
    # Status wins even though this is a confident archive in an archive category
    # from a VIP — the value must name the FIRST rule that stopped the agent.
    decision = _decision(status="needs_your_call", confidence=0.99)
    assert classify_autonomy_state(decision, NEWSLETTERS, SETTINGS) == "needs_your_call"


def test_an_error_tier_row_is_needs_your_call():
    decision = _decision(decided_by="error", category=None, confidence=0.0,
                         proposed_action="keep", status="proposed")
    assert classify_autonomy_state(decision, None, SETTINGS) == "needs_your_call"


def test_a_reviewer_flip_is_held_by_never_miss():
    decision = _decision(decided_by="reviewer", proposed_action="keep", confidence=0.9)
    assert classify_autonomy_state(decision, NEWSLETTERS, SETTINGS) == "held_by_never_miss"


def test_time_sensitive_is_held_by_never_miss_and_outranks_category_keep():
    decision = _decision(time_sensitive=True, proposed_action="keep", confidence=0.9,
                         category="people")
    assert classify_autonomy_state(decision, PEOPLE, SETTINGS) == "held_by_never_miss"


def test_a_vip_sender_is_held_by_never_miss():
    decision = _decision(proposed_action="keep", confidence=0.9)
    item = {"id": "i1", "from_email": "boss@acme.com", "from_domain": "acme.com",
            "subject": "hi"}
    state = classify_autonomy_state(
        decision, NEWSLETTERS, SETTINGS, {}, {"emails": ["boss@acme.com"]}, item=item
    )
    assert state == "held_by_never_miss"


def test_an_ever_replied_sender_is_held_by_never_miss():
    decision = _decision(proposed_action="keep", confidence=0.9)
    item = {"id": "i1", "from_email": "friend@acme.com", "from_domain": "acme.com",
            "subject": "hi"}
    state = classify_autonomy_state(
        decision,
        NEWSLETTERS,
        SETTINGS,
        {"friend@acme.com": {"ever_replied": True}},
        {},
        item=item,
    )
    assert state == "held_by_never_miss"


@pytest.mark.parametrize("category", [PEOPLE, URGENT])
def test_a_keep_category_is_category_keep_even_at_maximum_confidence(category):
    decision = _decision(category=category["key"], proposed_action="keep", confidence=0.99)
    assert classify_autonomy_state(decision, category, SETTINGS) == "category_keep"


def test_a_user_rule_keep_is_category_keep_because_the_user_outranks_the_default():
    decision = _decision(decided_by="rule", proposed_action="keep", confidence=0.99)
    assert classify_autonomy_state(decision, NEWSLETTERS, SETTINGS) == "category_keep"


def test_an_archive_at_or_above_the_bar_is_auto_act():
    assert classify_autonomy_state(_decision(confidence=0.80), NEWSLETTERS, SETTINGS) == "auto_act"


def test_an_archive_below_the_category_bar_is_below_threshold():
    # 0.83 clears the global 0.80 but not Outreach's stricter 0.85.
    decision = _decision(category="outreach", confidence=0.83)
    assert classify_autonomy_state(decision, OUTREACH, SETTINGS) == "below_threshold"
    assert classify_autonomy_state(
        _decision(category="outreach", confidence=0.86), OUTREACH, SETTINGS
    ) == "auto_act"


def test_a_digest_action_can_also_auto_act():
    category = {**NEWSLETTERS, "default_action": "digest"}
    decision = _decision(proposed_action="digest", confidence=0.9)
    assert classify_autonomy_state(decision, category, SETTINGS) == "auto_act"


def test_states_partition_a_mixed_run_with_no_row_left_unstamped():
    decisions = [
        _decision(item_id="a", confidence=0.9),
        _decision(item_id="b", confidence=0.76),
        _decision(item_id="c", status="needs_your_call"),
        _decision(item_id="d", category="people", proposed_action="keep", confidence=0.99),
        _decision(item_id="e", decided_by="reviewer", proposed_action="keep"),
    ]
    cats = {"newsletters": NEWSLETTERS, "people": PEOPLE}
    states = [
        classify_autonomy_state(d, cats.get(d["category"]), SETTINGS) for d in decisions
    ]
    assert states == [
        "auto_act",
        "below_threshold",
        "needs_your_call",
        "category_keep",
        "held_by_never_miss",
    ]
    assert all(s in AUTONOMY_STATES for s in states)


# --- Rule C5: cluster action refresh -----------------------------------------


def test_cluster_action_is_refreshed_from_its_members_final_actions():
    clusters = [{"label": "Substack", "item_ids": ["a", "b", "c"], "suggested_action": "keep"}]
    decisions = [
        {"item_id": "a", "proposed_action": "archive"},
        {"item_id": "b", "proposed_action": "archive"},
        {"item_id": "c", "proposed_action": "keep"},
    ]
    assert refresh_cluster_actions(clusters, decisions)[0]["suggested_action"] == "archive"


def test_a_tied_cluster_resolves_to_the_more_conservative_action():
    clusters = [{"item_ids": ["a", "b"], "suggested_action": "archive"}]
    decisions = [
        {"item_id": "a", "proposed_action": "archive"},
        {"item_id": "b", "proposed_action": "keep"},
    ]
    assert refresh_cluster_actions(clusters, decisions)[0]["suggested_action"] == "keep"


def test_a_cluster_with_no_resolvable_members_keeps_its_prior_action():
    clusters = [{"item_ids": ["gone"], "suggested_action": "archive"}]
    assert refresh_cluster_actions(clusters, [])[0]["suggested_action"] == "archive"


def test_refresh_cluster_actions_tolerates_empty_input():
    assert refresh_cluster_actions([], []) == []
    assert refresh_cluster_actions(None, None) == []
