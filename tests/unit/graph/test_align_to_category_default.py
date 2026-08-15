"""``align_to_category_default`` — the ONE stage allowed to move keep → archive.

spec/capabilities/drive-to-inbox-zero.md Rules C1-C5. Every exclusion in C1 is
tested individually, because the whole safety argument of Phase 7 rests on this
node being conservative: it runs *before* the reviewer and the never-miss floor,
so anything it converts is still audited — but anything it converts that it
should not have is mail the user asked to keep.
"""

from __future__ import annotations

from graph.nodes_autonomy import align_to_category_default, mark_autonomy_state

SETTINGS = {"auto_act_threshold": 0.80, "confidence_floor": 0.75}

CATEGORIES = [
    {"key": "newsletters", "name": "Newsletters", "default_action": "archive",
     "auto_act_threshold": None},
    {"key": "outreach", "name": "Outreach", "default_action": "archive",
     "auto_act_threshold": 0.85},
    {"key": "receipts", "name": "Receipts", "default_action": "archive",
     "auto_act_threshold": 0.85},
    {"key": "people", "name": "People", "default_action": "keep",
     "auto_act_threshold": None},
    {"key": "urgent", "name": "Urgent", "default_action": "keep",
     "auto_act_threshold": None},
    {"key": "digestible", "name": "Digestible", "default_action": "digest",
     "auto_act_threshold": None},
]


def _item(item_id="i1", email="news@substack.com", domain="substack.com", subject="Weekly"):
    return {"id": item_id, "from_email": email, "from_domain": domain, "subject": subject}


def _decision(**overrides) -> dict:
    base = {
        "item_id": "i1",
        "category": "newsletters",
        "proposed_action": "keep",
        "confidence": 0.88,
        "reasoning": "Looks like a bulk newsletter.",
        "decided_by": "llm",
        "rule_id": None,
        "time_sensitive": False,
        "unsure": False,
        "status": "proposed",
        "review_state": "provisional",
    }
    base.update(overrides)
    return base


def _state(decisions, *, items=None, sender_stats=None, vip=None, clusters=None):
    return {
        "run_id": "run-1",
        "user_id": "user-1",
        "settings": SETTINGS,
        "categories": CATEGORIES,
        "items": items if items is not None else [_item()],
        "sender_stats": sender_stats or {},
        "vip": vip or {},
        "decisions": decisions,
        "clusters": clusters or [],
    }


def _run(decisions, **kwargs) -> list[dict]:
    result = align_to_category_default(_state(decisions, **kwargs))
    assert result.get("error") is None
    return result["decisions"]


# --- the headline behaviour ---------------------------------------------------


def test_a_newsletters_keep_at_088_becomes_an_archive_proposal():
    out = _run([_decision(confidence=0.88)])[0]
    assert out["proposed_action"] == "archive"


def test_the_same_thread_at_079_stays_keep_and_is_marked_below_threshold():
    decisions = _run([_decision(confidence=0.79)])
    assert decisions[0]["proposed_action"] == "keep"

    stamped = mark_autonomy_state(_state(decisions))["decisions"]
    assert stamped[0]["autonomy_state"] == "below_threshold"


def test_the_reasoning_names_the_category_and_the_threshold():
    out = _run([_decision(confidence=0.88)])[0]
    assert "Newsletters" in out["reasoning"]
    assert "0.80" in out["reasoning"]
    # The original tier's reasoning is preserved, not replaced.
    assert "Looks like a bulk newsletter." in out["reasoning"]


def _assert_not_swept_as(out: dict, category: str) -> None:
    """The exclusion held: this thread was NOT realigned to `category`'s default.

    **Phase 9 update.** The exclusion has always meant "this thread is not swept
    as a <category>". Before Phase 9 the only way to express that was to leave it
    a `keep`; from Phase 9 a deterministic never-miss hold (VIP, ever-replied,
    time-sensitive) is instead proposed as an archive **under its own never-miss
    label**, precisely so the second-pass reviewer audits it — an unaudited row
    can never be mutated, so a never-miss archive that skipped the reviewer would
    silently never happen (`graph.nodes_autonomy._preresolve_never_miss_for_review`).

    What must never happen, and is asserted here, is the thread being swept as
    routine mail of its original category.
    """
    from graph.autonomy import NEVER_MISS_CATEGORY_KEYS

    if out["proposed_action"] == "keep":
        return
    assert out["category"] in NEVER_MISS_CATEGORY_KEYS, out
    assert out["category"] != category, (
        f"a never-miss thread must never be swept as a {category} — that is the "
        "exclusion this test exists for"
    )


def test_a_digest_category_converts_to_digest_not_archive():
    out = _run([_decision(category="digestible", confidence=0.9)])[0]
    assert out["proposed_action"] == "digest"


def test_outreach_respects_its_stricter_085_bar():
    below, above = _run([
        _decision(item_id="i1", category="outreach", confidence=0.83),
        _decision(item_id="i2", category="outreach", confidence=0.86),
    ], items=[_item("i1"), _item("i2")])
    assert below["proposed_action"] == "keep"
    assert above["proposed_action"] == "archive"


def test_receipts_moves_at_086_and_does_not_at_082_on_its_live_085_bar():
    # Phase 7 flipped Receipts from keep to archive, so its 0.85 seeded bar is
    # live: a notch stricter than the 0.80 global.
    below, above = _run([
        _decision(item_id="i1", category="receipts", confidence=0.82),
        _decision(item_id="i2", category="receipts", confidence=0.86),
    ], items=[_item("i1"), _item("i2")])
    assert below["proposed_action"] == "keep"
    assert above["proposed_action"] == "archive"


def test_a_time_sensitive_receipt_at_095_is_never_moved():
    # This is the safety argument for flipping Receipts to archive: the
    # never-miss layer, not the category default, is the right instrument for
    # the exception — a receipt that genuinely needs action stays visible.
    out = _run([_decision(category="receipts", confidence=0.95, time_sensitive=True)])[0]
    _assert_not_swept_as(out, "receipts")


# --- Rule C1: every exclusion, one at a time ---------------------------------


def test_exclusion_needs_your_call_is_never_realigned():
    out = _run([_decision(confidence=0.99, status="needs_your_call")])[0]
    assert out["proposed_action"] == "keep"


def test_exclusion_an_error_tier_row_is_never_realigned():
    out = _run([_decision(confidence=0.99, decided_by="error")])[0]
    assert out["proposed_action"] == "keep"


def test_exclusion_a_rule_decision_is_never_realigned_at_any_confidence():
    # Rule C4: a deterministic rule is the user's own instruction and outranks
    # every category default, in both directions.
    for confidence in (0.80, 0.95, 1.0):
        out = _run([_decision(confidence=confidence, decided_by="rule", rule_id="r1")])[0]
        assert out["proposed_action"] == "keep"
        assert out["decided_by"] == "rule"


def test_exclusion_time_sensitive_is_never_realigned():
    out = _run([_decision(confidence=0.99, time_sensitive=True)])[0]
    _assert_not_swept_as(out, "newsletters")


def test_exclusion_unsure_is_never_realigned():
    out = _run([_decision(confidence=0.99, unsure=True)])[0]
    assert out["proposed_action"] == "keep"


def test_exclusion_an_ever_replied_sender_is_never_realigned():
    out = _run(
        [_decision(confidence=0.99)],
        sender_stats={"news@substack.com": {"ever_replied": True}},
    )[0]
    _assert_not_swept_as(out, "newsletters")


def test_exclusion_a_vip_email_is_never_realigned():
    out = _run([_decision(confidence=0.99)], vip={"emails": ["news@substack.com"]})[0]
    _assert_not_swept_as(out, "newsletters")


def test_exclusion_a_vip_domain_is_never_realigned():
    out = _run([_decision(confidence=0.99)], vip={"domains": ["substack.com"]})[0]
    _assert_not_swept_as(out, "newsletters")


def test_exclusion_a_vip_keyword_is_never_realigned():
    out = _run([_decision(confidence=0.99)], vip={"keywords": ["weekly"]})[0]
    _assert_not_swept_as(out, "newsletters")


def test_exclusion_an_unknown_category_is_never_realigned():
    out = _run([_decision(category="not-a-real-category", confidence=0.99)])[0]
    assert out["proposed_action"] == "keep"


def test_people_and_urgent_at_099_are_never_moved():
    decisions = _run(
        [
            _decision(item_id="i1", category="people", confidence=0.99),
            _decision(item_id="i2", category="urgent", confidence=0.99),
        ],
        items=[_item("i1"), _item("i2")],
    )
    assert [d["proposed_action"] for d in decisions] == ["keep", "keep"]


# --- Rule C2: what the node must NOT touch -----------------------------------


def test_it_never_turns_an_archive_into_a_keep():
    out = _run([_decision(category="people", proposed_action="archive", confidence=0.99)])[0]
    assert out["proposed_action"] == "archive"


def test_it_never_changes_confidence_decided_by_status_or_review_state():
    before = _decision(confidence=0.88)
    after = _run([dict(before)])[0]
    assert after["proposed_action"] == "archive"  # it did do its job
    for field in ("confidence", "decided_by", "status", "review_state", "item_id",
                  "category", "time_sensitive", "rule_id"):
        assert after[field] == before[field], field


def test_the_input_decision_dicts_are_not_mutated_in_place():
    original = _decision(confidence=0.88)
    _run([original])
    assert original["proposed_action"] == "keep"


# --- Rule C5: cluster refresh -------------------------------------------------


def test_cluster_suggested_action_is_refreshed_after_realignment():
    clusters = [{"label": "Substack", "item_ids": ["i1", "i2"], "suggested_action": "keep"}]
    result = align_to_category_default(
        _state(
            [
                _decision(item_id="i1", confidence=0.9),
                _decision(item_id="i2", confidence=0.9),
            ],
            items=[_item("i1"), _item("i2")],
            clusters=clusters,
        )
    )
    assert result["clusters"][0]["suggested_action"] == "archive"
    # The node works on a copy; the caller's cluster list is untouched until it
    # takes the node's return value, which is the authoritative snapshot.
    assert clusters[0]["suggested_action"] == "keep"


# --- robustness ---------------------------------------------------------------


def test_a_malformed_state_surfaces_as_state_error_not_an_exception():
    result = align_to_category_default({"decisions": "not-a-list", "categories": []})
    assert result.get("error")
    assert "align_to_category_default failed" in result["error"]


def test_an_empty_run_is_a_no_op():
    result = align_to_category_default(_state([]))
    assert result["decisions"] == []
    assert result["error"] is None


def test_mark_autonomy_state_stamps_every_decision_exactly_once():
    decisions = _run(
        [
            _decision(item_id="i1", confidence=0.9),
            _decision(item_id="i2", confidence=0.5, status="needs_your_call"),
            _decision(item_id="i3", category="people", confidence=0.99),
        ],
        items=[_item("i1"), _item("i2"), _item("i3")],
    )
    stamped = mark_autonomy_state(_state(decisions, items=[_item("i1"), _item("i2"), _item("i3")]))
    assert [d["autonomy_state"] for d in stamped["decisions"]] == [
        "auto_act",
        "needs_your_call",
        "category_keep",
    ]


def test_mark_autonomy_state_surfaces_failure_as_state_error():
    result = mark_autonomy_state({"decisions": "nope", "categories": []})
    assert result.get("error")
    assert "mark_autonomy_state failed" in result["error"]
