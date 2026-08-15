"""Phase 9, slice 3 — a never-miss verdict resolves to a LABEL, deterministically.

`spec/capabilities/never-miss-safeguards.md` § *Phase 9 — how a never-miss verdict
is expressed*. Four rules, first match wins, no LLM:

1. VIP match or `ever_replied` -> `people`
2. `time_sensitive`            -> `urgent`
3. any other reviewer hold     -> `important`
4. nothing resolves            -> `None`, and the thread STAYS IN THE INBOX

Rule 4 is the one that matters most. Silence is never the fallback: an
unresolvable label costs us a zero, never a thread.

This file also pins the reconciliation the whole phase rests on
(`§ Reconciling NEVER_ARCHIVE_KEYS with the reframe`): a category-wide
`default_action="archive"` on People/Urgent/Legal/Important is still refused —
it is a bulk silent sweep of the mail a human must see — while the *same*
categories happily receive per-thread never-miss archives. Those are different
operations, and only one of them was ever forbidden.
"""

from __future__ import annotations

import pytest

from graph.autonomy import (
    IMPORTANT_KEY,
    NEVER_MISS_CATEGORY_KEYS,
    PEOPLE_KEY,
    URGENT_KEY,
    classify_autonomy_state,
    never_miss_category_key,
)
from graph.nodes_autonomy import mark_autonomy_state


def _item(item_id: str = "it-1", *, from_email: str = "someone@example.com") -> dict:
    return {
        "id": item_id,
        "from_email": from_email,
        "from_domain": from_email.split("@")[-1],
        "subject": "a subject",
        "snippet_redacted": "snippet",
    }


def _decision(**fields) -> dict:
    base = {
        "item_id": "it-1",
        "category": "notifications",
        "proposed_action": "keep",
        "confidence": 0.88,
        "reasoning": "because",
        "decided_by": "llm",
        "time_sensitive": False,
        "status": "proposed",
    }
    base.update(fields)
    return base


# --------------------------------------------------------------------------
# The resolution table
# --------------------------------------------------------------------------


def test_reply_history_resolves_to_people():
    key = never_miss_category_key(
        _decision(decided_by="sender_history"),
        _item(from_email="maya@northwind.io"),
        vip={},
        sender_stats={"maya@northwind.io": {"ever_replied": True}},
    )
    assert key == PEOPLE_KEY


def test_vip_resolves_to_people():
    key = never_miss_category_key(
        _decision(),
        _item(from_email="boss@acme.com"),
        vip={"emails": ["boss@acme.com"]},
        sender_stats={},
    )
    assert key == PEOPLE_KEY


def test_time_sensitive_resolves_to_urgent():
    key = never_miss_category_key(
        _decision(time_sensitive=True),
        _item(from_email="no-reply@accounts.google.com"),
        vip={},
        sender_stats={},
    )
    assert key == URGENT_KEY


def test_a_reviewer_hold_for_any_other_reason_resolves_to_important():
    key = never_miss_category_key(
        _decision(decided_by="reviewer"), _item(), vip={}, sender_stats={}
    )
    assert key == IMPORTANT_KEY


def test_precedence_people_beats_urgent_beats_important():
    """First match wins: the label names the strongest thing known, not the last guard."""
    everything = _decision(decided_by="reviewer", time_sensitive=True)
    item = _item(from_email="maya@northwind.io")
    stats = {"maya@northwind.io": {"ever_replied": True}}

    assert never_miss_category_key(everything, item, vip={}, sender_stats=stats) == PEOPLE_KEY
    assert (
        never_miss_category_key(everything, item, vip={}, sender_stats={}) == URGENT_KEY
    )
    assert (
        never_miss_category_key(
            _decision(decided_by="reviewer"), item, vip={}, sender_stats={}
        )
        == IMPORTANT_KEY
    )


def test_nothing_resolves_returns_none_rather_than_guessing():
    """The unresolvable case. `None` is an answer, and it means "stay in the inbox"."""
    assert (
        never_miss_category_key(_decision(), _item(), vip={}, sender_stats={}) is None
    )


def test_a_no_reply_sender_is_still_eligible_for_a_time_sensitive_label():
    """Correspondent truth removes a *reply-history* claim, never the mail's urgency.

    A no-reply security alert must still reach `ZeroInbox/Urgent` — downgrading it
    would be the never-miss layer losing exactly the mail it exists for.
    """
    for sender in (
        "no_reply@email.apple.com",
        "noreply@email.apple.com",
        "no-reply@accounts.google.com",
    ):
        assert (
            never_miss_category_key(
                _decision(time_sensitive=True),
                _item(from_email=sender),
                vip={},
                sender_stats={},
            )
            == URGENT_KEY
        )


def test_the_three_never_miss_keys_are_the_documented_three():
    assert NEVER_MISS_CATEGORY_KEYS == (PEOPLE_KEY, URGENT_KEY, IMPORTANT_KEY)


# --------------------------------------------------------------------------
# The node: the verdict becomes a labelled archive
# --------------------------------------------------------------------------


def _categories() -> list[dict]:
    return [
        {"key": "notifications", "name": "Notifications", "default_action": "archive",
         "channel_label_name": "ZeroInbox/Notifications", "auto_act_threshold": None},
        {"key": "people", "name": "People", "default_action": "keep",
         "channel_label_name": "ZeroInbox/People", "auto_act_threshold": None},
        {"key": "urgent", "name": "Urgent", "default_action": "keep",
         "channel_label_name": "ZeroInbox/Urgent", "auto_act_threshold": None},
        {"key": "important", "name": "Important", "default_action": "keep",
         "channel_label_name": "ZeroInbox/Important", "auto_act_threshold": None},
    ]


def _state(decisions: list[dict], items: list[dict], **extra) -> dict:
    state = {
        "decisions": decisions,
        "items": items,
        "categories": _categories(),
        "settings": {"confidence_floor": 0.75, "auto_act_threshold": 0.80},
        "sender_stats": {},
        "vip": {},
    }
    state.update(extra)
    return state


def _by_id(out: dict) -> dict[str, dict]:
    return {d["item_id"]: d for d in out["decisions"]}


def test_a_time_sensitive_hold_becomes_an_archive_under_urgent():
    out = mark_autonomy_state(
        _state([_decision(item_id="it-1", time_sensitive=True)], [_item("it-1")])
    )
    decision = _by_id(out)["it-1"]

    assert decision["autonomy_state"] == "held_by_never_miss", (
        "the verdict is unchanged — only its expression is; the ledger must still "
        "say WHY the thread was held"
    )
    assert decision["proposed_action"] == "archive"
    assert decision["category"] == URGENT_KEY
    assert decision["never_miss_label"] == "ZeroInbox/Urgent"
    assert "ZeroInbox/Urgent" in decision["reasoning"], (
        "the reasoning must name the label in plain words, so the user is never "
        "left guessing where their mail went"
    )


def test_a_reviewer_hold_becomes_an_archive_under_important():
    out = mark_autonomy_state(
        _state(
            [_decision(item_id="it-1", decided_by="reviewer", proposed_action="keep")],
            [_item("it-1")],
        )
    )
    decision = _by_id(out)["it-1"]
    assert decision["proposed_action"] == "archive"
    assert decision["category"] == IMPORTANT_KEY
    assert decision["decided_by"] == "reviewer", "the reframe never rewrites the verdict"


def test_a_reply_history_hold_becomes_an_archive_under_people():
    out = mark_autonomy_state(
        _state(
            [_decision(item_id="it-1", decided_by="sender_history")],
            [_item("it-1", from_email="maya@northwind.io")],
            sender_stats={"maya@northwind.io": {"ever_replied": True}},
        )
    )
    decision = _by_id(out)["it-1"]
    assert decision["proposed_action"] == "archive"
    assert decision["category"] == PEOPLE_KEY


def test_an_unresolvable_never_miss_thread_is_left_in_the_inbox():
    """The user deleted `Important`. The thread is NOT archived — it stays put."""
    categories = [c for c in _categories() if c["key"] != IMPORTANT_KEY]
    out = mark_autonomy_state(
        _state(
            [_decision(item_id="it-1", decided_by="reviewer", proposed_action="keep")],
            [_item("it-1")],
            categories=categories,
        )
    )
    decision = _by_id(out)["it-1"]

    assert decision["proposed_action"] == "keep", (
        "a never-miss thread with no resolvable label must never become a bare "
        "archive — that is the one outcome this product exists to prevent"
    )
    assert decision["never_miss_label"] is None
    assert decision["autonomy_state"] == "held_by_never_miss"


def test_needs_your_call_and_below_threshold_are_untouched_by_the_reframe():
    """The floor still binds. The reframe archives mail the agent was confident
    *matters* — never mail it was not confident about."""
    below_floor = _decision(item_id="it-floor", confidence=0.40, proposed_action="keep",
                            status="needs_your_call")
    below_bar = _decision(item_id="it-bar", confidence=0.78, proposed_action="archive")
    out = mark_autonomy_state(
        _state([below_floor, below_bar], [_item("it-floor"), _item("it-bar")])
    )
    decisions = _by_id(out)

    assert decisions["it-floor"]["autonomy_state"] == "needs_your_call"
    assert decisions["it-floor"]["proposed_action"] == "keep"
    assert "never_miss_label" not in decisions["it-floor"]

    assert decisions["it-bar"]["autonomy_state"] == "below_threshold"
    assert decisions["it-bar"]["category"] == "notifications"
    assert "never_miss_label" not in decisions["it-bar"]


def test_auto_act_decisions_keep_their_own_category():
    """A confident archive in an archive-by-default category is not a never-miss
    archive and must never be re-filed under a never-miss label."""
    out = mark_autonomy_state(
        _state([_decision(item_id="it-1", proposed_action="archive")], [_item("it-1")])
    )
    decision = _by_id(out)["it-1"]
    assert decision["autonomy_state"] == "auto_act"
    assert decision["category"] == "notifications"


def test_classify_autonomy_state_itself_is_unchanged_by_the_reframe():
    """`held_by_never_miss` still names the reason. It now describes why the thread
    was LABELLED rather than why it stayed — the value is the same either way."""
    assert (
        classify_autonomy_state(
            _decision(time_sensitive=True),
            {"key": "notifications", "default_action": "archive"},
            {"confidence_floor": 0.75, "auto_act_threshold": 0.80},
            {},
            {},
            item=_item(),
        )
        == "held_by_never_miss"
    )


# --------------------------------------------------------------------------
# NEVER_ARCHIVE_KEYS, reconciled — kept and extended, not deleted
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["people", "urgent", "legal", "important"])
def test_never_archive_keys_still_reject_a_category_wide_archive_default(
    key, _isolated_db
):
    """On create AND on update. This is the guard found live on a real account
    (commit 6e37375); Phase 9 extends it with `important` rather than removing it."""
    from db.models import User
    from db.session import create_db_session
    from tools.taxonomy import NEVER_ARCHIVE_KEYS, TaxonomyError, create_category, update_category

    assert key in NEVER_ARCHIVE_KEYS, (
        f"{key!r} must be a NEVER_ARCHIVE_KEYS member — a category-wide archive "
        "default on it is a bulk silent sweep of mail a human must see"
    )

    user_id = "test-never-archive"
    with create_db_session() as session:
        session.add(User(id=user_id, email="nk@example.com", display_name="N"))
        session.flush()

        with pytest.raises(TaxonomyError):
            create_category(session, user_id, key=key, name=key.title(),
                            default_action="archive")

        category = create_category(session, user_id, key=key, name=key.title(),
                                   default_action="keep")
        with pytest.raises(TaxonomyError):
            update_category(session, user_id, category.id, default_action="archive")

        session.rollback()


def test_the_same_categories_are_reachable_by_a_never_miss_archive():
    """The distinction the whole phase rests on: the guard blocks a category-wide
    sweep and has never blocked a per-thread, labelled, undoable never-miss archive."""
    from tools.taxonomy import NEVER_ARCHIVE_KEYS

    assert set(NEVER_MISS_CATEGORY_KEYS) <= set(NEVER_ARCHIVE_KEYS), (
        "every never-miss label must also be a category that can never be swept "
        "as noise — otherwise the reframe would file mail into a category the "
        "taxonomy editor could then set to archive-by-default"
    )
