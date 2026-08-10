"""Unit coverage for the three never-miss mechanisms (spec/capabilities/never-miss-safeguards.md).

Fast, mocked-LLM tests — no real API calls. The real-endpoint, full-fixture proof
lives in tests/integration/test_never_miss.py.
"""

from __future__ import annotations

import pytest

from graph import nodes_review
from tools import never_miss


def _decision(**overrides) -> dict:
    base = {
        "item_id": "item-1",
        "category": "newsletters",
        "proposed_action": "archive",
        "confidence": 0.9,
        "reasoning": "Looks like a bulk newsletter.",
        "decided_by": "llm",
        "rule_id": None,
        "time_sensitive": False,
    }
    base.update(overrides)
    return base


def _item(item_id="item-1", from_email="sender@example.com", **overrides) -> dict:
    base = {
        "id": item_id,
        "from_name": "Sender",
        "from_email": from_email,
        "from_domain": from_email.split("@")[-1],
        "subject": "Weekly digest",
        "snippet_redacted": "Some content.",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------- Mechanism B


class TestConfidenceFloor:
    def test_below_floor_forces_keep_and_needs_your_call(self):
        decisions = [_decision(confidence=0.5)]
        out = never_miss.apply_confidence_floor(decisions, 0.75)
        assert out[0]["proposed_action"] == "keep"
        assert out[0]["status"] == "needs_your_call"
        assert "0.50" in out[0]["reasoning"]
        assert "0.75" in out[0]["reasoning"]

    def test_at_or_above_floor_is_left_alone(self):
        decisions = [_decision(confidence=0.75), _decision(item_id="item-2", confidence=0.99)]
        out = never_miss.apply_confidence_floor(decisions, 0.75)
        assert [d["proposed_action"] for d in out] == ["archive", "archive"]
        assert [d["status"] for d in out] == ["proposed", "proposed"]

    def test_a_decision_already_keep_below_floor_is_only_flagged_needs_your_call(self):
        decisions = [_decision(proposed_action="keep", confidence=0.1)]
        out = never_miss.apply_confidence_floor(decisions, 0.75)
        assert out[0]["proposed_action"] == "keep"
        assert out[0]["status"] == "needs_your_call"
        # No spurious "Confidence ... floor" sentence appended when it was already keep.
        assert out[0]["reasoning"] == decisions[0]["reasoning"]


# --------------------------------------------------------------- Mechanism C


class TestReplyHistoryGuard:
    def test_ever_replied_sender_archive_proposal_is_forced_to_keep(self):
        decisions = [_decision()]
        items = [_item()]
        sender_stats = {"sender@example.com": {"ever_replied": True}}
        out = never_miss.apply_reply_history_guard(decisions, sender_stats, items)
        assert out[0]["proposed_action"] == "keep"
        assert "replied to sender@example.com" in out[0]["reasoning"]

    def test_never_replied_sender_is_left_alone(self):
        decisions = [_decision()]
        items = [_item()]
        sender_stats = {"sender@example.com": {"ever_replied": False}}
        out = never_miss.apply_reply_history_guard(decisions, sender_stats, items)
        assert out[0]["proposed_action"] == "archive"

    def test_explicit_override_lets_the_archive_stand(self):
        decisions = [_decision()]
        items = [_item()]
        sender_stats = {"sender@example.com": {"ever_replied": True}}
        out = never_miss.apply_reply_history_guard(
            decisions, sender_stats, items, overrides={"sender@example.com"}
        )
        assert out[0]["proposed_action"] == "archive"

    def test_keep_decisions_are_untouched_even_for_ever_replied_senders(self):
        decisions = [_decision(proposed_action="keep")]
        items = [_item()]
        sender_stats = {"sender@example.com": {"ever_replied": True}}
        out = never_miss.apply_reply_history_guard(decisions, sender_stats, items)
        assert out[0] == decisions[0]

    def test_unknown_sender_with_no_stats_is_left_alone(self):
        decisions = [_decision()]
        items = [_item()]
        out = never_miss.apply_reply_history_guard(decisions, {}, items)
        assert out[0]["proposed_action"] == "archive"


# -------------------------------------------------------------- VIP guard


class TestVipGuard:
    def test_email_vip_match_forces_archive_to_keep(self):
        decisions = [_decision()]
        items = [_item(from_email="vip@example.com")]
        vip = {"emails": ["vip@example.com"], "domains": [], "keywords": []}
        out = never_miss.apply_vip_guard(decisions, vip, items)
        assert out[0]["proposed_action"] == "keep"
        assert "VIP list" in out[0]["reasoning"]

    def test_domain_vip_match_forces_archive_to_keep(self):
        decisions = [_decision()]
        items = [_item(from_email="anyone@partner.io")]
        vip = {"emails": [], "domains": ["partner.io"], "keywords": []}
        out = never_miss.apply_vip_guard(decisions, vip, items)
        assert out[0]["proposed_action"] == "keep"

    def test_keyword_vip_match_in_subject_forces_archive_to_keep(self):
        decisions = [_decision()]
        items = [_item(subject="Urgent: board meeting agenda")]
        vip = {"emails": [], "domains": [], "keywords": ["board meeting"]}
        out = never_miss.apply_vip_guard(decisions, vip, items)
        assert out[0]["proposed_action"] == "keep"

    def test_no_match_leaves_archive_decision_unchanged(self):
        decisions = [_decision()]
        items = [_item()]
        vip = {"emails": ["other@example.com"], "domains": ["other.io"], "keywords": ["investor"]}
        out = never_miss.apply_vip_guard(decisions, vip, items)
        assert out[0]["proposed_action"] == "archive"

    def test_empty_vip_dict_is_a_no_op(self):
        decisions = [_decision()]
        items = [_item()]
        out = never_miss.apply_vip_guard(decisions, {}, items)
        assert out[0]["proposed_action"] == "archive"

    def test_keep_decisions_are_untouched_even_on_vip_match(self):
        decisions = [_decision(proposed_action="keep")]
        items = [_item(from_email="vip@example.com")]
        vip = {"emails": ["vip@example.com"], "domains": [], "keywords": []}
        out = never_miss.apply_vip_guard(decisions, vip, items)
        # proposed_action stays keep but no reasoning is appended
        assert out[0]["proposed_action"] == "keep"
        assert "VIP list" not in out[0]["reasoning"]


# --------------------------------------------------------------- Mechanism A


class _FakeResult:
    def __init__(self, results, usage=None):
        self.results = results
        self.usage = usage


class _FakeUsage:
    model = "fake-model"
    tokens_in = 100
    tokens_out = 20
    usd = 0.001
    latency_ms = 250


class TestSecondPassReviewer:
    def test_flips_only_the_verdict_the_reviewer_names(self, monkeypatch):
        class FakeClient:
            async def classify_batch(self, items, **kwargs):
                return _FakeResult(
                    [
                        {"item_id": "item-1", "flip": True, "reasoning": "It's a signed contract deadline."},
                        {"item_id": "item-2", "flip": False, "reasoning": "Genuinely a newsletter."},
                    ],
                    usage=_FakeUsage(),
                )

        monkeypatch.setattr("llm.client.get_llm_client", lambda: FakeClient())

        state = {
            "decisions": [
                _decision(item_id="item-1"),
                _decision(item_id="item-2"),
            ],
            "items": [_item("item-1"), _item("item-2", from_email="other@example.com")],
            "settings": {},
        }
        out = nodes_review.second_pass_reviewer(state)
        decisions = {d["item_id"]: d for d in out["decisions"]}
        assert decisions["item-1"]["proposed_action"] == "keep"
        assert decisions["item-1"]["decided_by"] == "reviewer"
        assert "Reviewer:" in decisions["item-1"]["reasoning"]
        assert decisions["item-2"]["proposed_action"] == "archive"
        assert decisions["item-2"]["decided_by"] == "llm"
        assert out["llm_calls"] and out["llm_calls"][0]["purpose"] == "review"

    def test_never_flips_keep_to_archive(self, monkeypatch):
        """The reviewer schema has no archive-proposing path — only items already
        proposed for archive are ever sent to it, so a 'keep' decision cannot be
        touched even if a malformed reply tried to claim otherwise."""
        called_with = {}

        class FakeClient:
            async def classify_batch(self, items, **kwargs):
                called_with["items"] = items
                return _FakeResult([], usage=_FakeUsage())

        monkeypatch.setattr("llm.client.get_llm_client", lambda: FakeClient())

        state = {
            "decisions": [_decision(item_id="item-1", proposed_action="keep")],
            "items": [_item("item-1")],
            "settings": {},
        }
        out = nodes_review.second_pass_reviewer(state)
        assert out["decisions"][0]["proposed_action"] == "keep"
        assert "items" not in called_with  # the reviewer was never even called

    def test_persistent_failure_routes_every_unreviewed_archive_to_needs_your_call(
        self, monkeypatch
    ):
        class BoomClient:
            async def classify_batch(self, items, **kwargs):
                raise RuntimeError("NIM endpoint unreachable")

        monkeypatch.setattr("llm.client.get_llm_client", lambda: BoomClient())

        state = {
            "decisions": [_decision(item_id="item-1"), _decision(item_id="item-2")],
            "items": [_item("item-1"), _item("item-2")],
            "settings": {},
        }
        out = nodes_review.second_pass_reviewer(state)
        for decision in out["decisions"]:
            assert decision["proposed_action"] == "archive"
            assert decision["status"] == "needs_your_call"

    def test_decisions_already_needs_your_call_are_never_sent_to_the_reviewer(self, monkeypatch):
        called = {"count": 0}

        class FakeClient:
            async def classify_batch(self, items, **kwargs):
                called["count"] += 1
                return _FakeResult([], usage=_FakeUsage())

        monkeypatch.setattr("llm.client.get_llm_client", lambda: FakeClient())

        state = {
            "decisions": [
                _decision(item_id="item-1", status="needs_your_call", proposed_action="keep")
            ],
            "items": [_item("item-1")],
            "settings": {},
        }
        out = nodes_review.second_pass_reviewer(state)
        assert called["count"] == 0
        assert out["decisions"] == state["decisions"]


# ------------------------------------------------------------- Combined node


class TestApplyNeverMissFloor:
    def test_applies_floor_then_reply_history_in_order(self):
        state = {
            "decisions": [
                _decision(item_id="item-1", confidence=0.5),  # below floor -> needs_your_call
                _decision(item_id="item-2", confidence=0.9),  # ever-replied -> forced keep
            ],
            "items": [_item("item-1"), _item("item-2", from_email="friend@example.com")],
            "sender_stats": {"friend@example.com": {"ever_replied": True}},
            "settings": {"confidence_floor": 0.75},
        }
        out = nodes_review.apply_never_miss_floor(state)
        decisions = {d["item_id"]: d for d in out["decisions"]}
        assert decisions["item-1"]["status"] == "needs_your_call"
        assert decisions["item-1"]["proposed_action"] == "keep"
        assert decisions["item-2"]["proposed_action"] == "keep"
        assert decisions["item-2"]["status"] == "proposed"

    def test_default_floor_is_used_when_settings_omit_it(self):
        state = {
            "decisions": [_decision(confidence=0.5)],
            "items": [_item()],
            "sender_stats": {},
            "settings": {},
        }
        out = nodes_review.apply_never_miss_floor(state)
        assert out["decisions"][0]["status"] == "needs_your_call"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
