"""Tier 1 (deterministic rules) and tier 2 (sender history) — the free tiers."""

from datetime import datetime, timedelta, timezone

import pytest

from tools.rules import (
    DEFAULT_TAXONOMY,
    apply_rules,
    apply_sender_history,
    matches,
    sender_history_decision,
)


def item(**overrides):
    base = {
        "id": "i1",
        "subject": "Weekly digest",
        "from_email": "news@substack.com",
        "from_domain": "substack.com",
        "list_id": "<weekly.substack.com>",
        "has_attachments": False,
        "internal_date": datetime.now(timezone.utc),
    }
    base.update(overrides)
    return base


def rule(**overrides):
    base = {
        "id": "r1",
        "name": "Substack newsletters",
        "matcher": {"list_id": "substack.com"},
        "action": {"set_category": "newsletters", "archive": True},
        "status": "active",
        "confidence": 0.96,
    }
    base.update(overrides)
    return base


class TestMatchers:
    def test_list_id_substring_match(self):
        assert matches(item(), {"list_id": "substack.com"})

    def test_from_email_is_case_insensitive(self):
        assert matches(item(from_email="News@Substack.com"), {"from_email": "news@substack.com"})

    def test_domain_matches_subdomains(self):
        assert matches(item(from_domain="mail.github.com"), {"from_domain": "github.com"})

    def test_domain_does_not_match_lookalike(self):
        assert not matches(item(from_domain="notgithub.com"), {"from_domain": "github.com"})

    def test_subject_regex(self):
        assert matches(item(subject="Your receipt from Stripe"), {"subject_regex": r"receipt"})
        assert not matches(item(subject="Hello"), {"subject_regex": r"receipt"})

    def test_all_clauses_must_match(self):
        assert not matches(
            item(), {"list_id": "substack.com", "from_domain": "github.com"}
        )

    def test_has_attachment_clause(self):
        assert matches(item(has_attachments=True), {"has_attachment": True})
        assert not matches(item(has_attachments=False), {"has_attachment": True})

    def test_older_than_days(self):
        old = item(internal_date=datetime.now(timezone.utc) - timedelta(days=40))
        assert matches(old, {"older_than_days": 30})
        assert not matches(item(), {"older_than_days": 30})

    def test_empty_matcher_never_fires(self):
        assert not matches(item(), {})

    def test_invalid_regex_does_not_raise(self):
        assert matches(item(), {"subject_regex": "([unclosed"}) is False

    def test_missing_field_does_not_match(self):
        assert not matches(item(list_id=None), {"list_id": "substack.com"})


class TestApplyRules:
    def test_matching_rule_produces_a_rule_tier_decision(self):
        decisions, unresolved = apply_rules([item()], [rule()])
        assert unresolved == []
        (decision,) = decisions
        assert decision["decided_by"] == "rule"
        assert decision["rule_id"] == "r1"
        assert decision["proposed_action"] == "archive"
        assert decision["category"] == "newsletters"
        assert decision["confidence"] == 0.96
        assert "Substack newsletters" in decision["reasoning"]

    def test_unmatched_item_is_left_for_later_tiers(self):
        decisions, unresolved = apply_rules([item(list_id=None, from_domain="acme.io")], [rule()])
        assert decisions == []
        assert len(unresolved) == 1

    def test_disabled_and_proposed_rules_never_fire(self):
        for status in ("proposed", "disabled"):
            decisions, unresolved = apply_rules([item()], [rule(status=status)])
            assert decisions == [] and len(unresolved) == 1

    def test_first_matching_rule_wins(self):
        first = rule(id="r1", action={"set_category": "newsletters", "archive": True})
        second = rule(id="r2", action={"set_category": "notifications", "archive": False})
        decisions, _ = apply_rules([item()], [first, second])
        assert decisions[0]["rule_id"] == "r1"

    def test_no_rules_leaves_everything_unresolved(self):
        decisions, unresolved = apply_rules([item(), item(id="i2")], [])
        assert decisions == [] and len(unresolved) == 2

    def test_empty_input(self):
        assert apply_rules([], [rule()]) == ([], [])


class TestSenderHistory:
    def test_ever_replied_sender_is_kept_at_high_confidence(self):
        decision = sender_history_decision(
            item(from_email="friend@acme.io"),
            {"ever_replied": True, "replied_count": 4, "received_count": 9},
        )
        assert decision["proposed_action"] == "keep"
        assert decision["decided_by"] == "sender_history"
        assert decision["confidence"] >= 0.95
        assert "replied" in decision["reasoning"]

    def test_never_opened_bulk_sender_is_proposed_for_archive(self):
        decision = sender_history_decision(
            item(),
            {
                "ever_replied": False,
                "received_count": 22,
                "opened_count": 0,
                "archived_by_user_count": 18,
            },
        )
        assert decision["proposed_action"] == "archive"
        assert decision["decided_by"] == "sender_history"

    def test_ever_replied_beats_bulk_archive_evidence(self):
        decision = sender_history_decision(
            item(),
            {
                "ever_replied": True,
                "received_count": 22,
                "opened_count": 0,
                "archived_by_user_count": 18,
            },
        )
        assert decision["proposed_action"] == "keep"

    @pytest.mark.parametrize(
        "stats",
        [
            None,
            {},
            {"received_count": 2, "opened_count": 0, "archived_by_user_count": 2},
            {"received_count": 30, "opened_count": 12, "archived_by_user_count": 20},
        ],
    )
    def test_weak_evidence_is_inconclusive(self, stats):
        assert sender_history_decision(item(), stats) is None

    def test_apply_sender_history_partitions_the_queue(self):
        items = [item(id="a", from_email="friend@acme.io"), item(id="b", from_email="x@y.io")]
        stats = {"friend@acme.io": {"ever_replied": True, "replied_count": 1}}
        decisions, unresolved = apply_sender_history(items, stats)
        assert [d["item_id"] for d in decisions] == ["a"]
        assert [i["id"] for i in unresolved] == ["b"]


def test_default_taxonomy_is_the_specified_six():
    assert [c["key"] for c in DEFAULT_TAXONOMY] == [
        "newsletters",
        "notifications",
        "receipts",
        "outreach",
        "people",
        "urgent",
    ]
    assert all(c["description"] for c in DEFAULT_TAXONOMY)
