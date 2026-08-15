"""Correspondent truth: no-reply detection and self-address normalisation.

Fails against pre-Phase-9 code — ``tools.correspondents`` did not exist, and the
reply-history guard honoured an ``ever_replied`` claim from any address at all.
"""

import pytest

from tools.correspondents import (
    NO_REPLY_PATTERNS,
    is_no_reply,
    is_self_address,
    normalize_address,
)
from tools.never_miss import apply_reply_history_guard, is_genuine_correspondent

# The five no-reply senders measured on the live account as the top never-miss
# holds (44 + 39 + 39 + 27 + 11 = 160 of the 227 held threads).
LIVE_NO_REPLY_SENDERS = [
    "no_reply@email.apple.com",
    "noreply@email.apple.com",
    "no-reply@accounts.google.com",
    "no-reply@entertainment.bookmyshow.com",
    "no-reply@updates.bookmyshow.com",
]


class TestIsNoReply:
    @pytest.mark.parametrize("address", LIVE_NO_REPLY_SENDERS)
    def test_every_measured_live_no_reply_sender_matches(self, address):
        assert is_no_reply(address) is True

    @pytest.mark.parametrize(
        "address",
        [
            "noreply@x.com",
            "no-reply@x.com",
            "no_reply@x.com",
            "no.reply@x.com",
            "NoReply@X.com",
            "NO-REPLY@X.COM",
            "donotreply@x.com",
            "do-not-reply@x.com",
            "do_not_reply@x.com",
            "Do.Not.Reply@x.com",
            "bounce-noreply@x.com",
            "Apple <no_reply@email.apple.com>",
        ],
    )
    def test_the_full_pattern_matrix_matches(self, address):
        assert is_no_reply(address) is True

    @pytest.mark.parametrize(
        "address",
        [
            "reply@x.com",
            "replies@x.com",
            "ada@example.com",
            "contact@jagrititheatre.com",
            "notification@facebookmail.com",
            "support@x.com",
            "norepose@x.com",
            "",
        ],
    )
    def test_normal_addresses_must_not_match(self, address):
        assert is_no_reply(address) is False

    def test_noreplyneeded_matches_by_design(self):
        # Documented, deliberate false positive: the substring rule is the
        # conservative choice — losing a never-miss signal costs a signal, while
        # honouring a fake one holds a thread in the inbox forever.
        assert is_no_reply("noreplyneeded@example.com") is True

    def test_patterns_are_a_public_frozen_tuple(self):
        assert isinstance(NO_REPLY_PATTERNS, tuple)
        assert "noreply" in NO_REPLY_PATTERNS and "do-not-reply" in NO_REPLY_PATTERNS


class TestIsSelfAddress:
    ACCOUNT = "psykrsna@gmail.com"

    @pytest.mark.parametrize(
        "address",
        [
            "psykrsna@gmail.com",
            "psy.krsna@gmail.com",
            "p.s.y.k.r.s.n.a@gmail.com",
            "psykrsna+news@gmail.com",
            "psy.krsna+news@gmail.com",
            "PsyKrsna@Gmail.com",
            "psykrsna@googlemail.com",
        ],
    )
    def test_every_spelling_of_the_users_own_address_is_self(self, address):
        assert is_self_address(address, account_email=self.ACCOUNT, aliases=[]) is True

    def test_a_send_as_alias_is_self(self):
        assert (
            is_self_address(
                "sai+work@madhyamakist.com",
                account_email=self.ACCOUNT,
                aliases=["sai@madhyamakist.com"],
            )
            is True
        )

    @pytest.mark.parametrize(
        "address", ["ada@example.com", "psykrsna@example.com", "psykrsna2@gmail.com", ""]
    )
    def test_other_people_are_not_self(self, address):
        assert is_self_address(address, account_email=self.ACCOUNT, aliases=[]) is False

    def test_dots_are_significant_outside_gmail(self):
        assert (
            is_self_address(
                "firstlast@company.com", account_email="first.last@company.com", aliases=[]
            )
            is False
        )

    def test_no_account_email_means_nothing_is_self(self):
        assert is_self_address("anyone@x.com", account_email="", aliases=[]) is False

    def test_normalize_address_is_idempotent(self):
        assert normalize_address("Psy.Krsna+tag@Gmail.com") == "psykrsna@gmail.com"
        assert normalize_address(normalize_address("Psy.Krsna+tag@Gmail.com")) == (
            "psykrsna@gmail.com"
        )


class TestGenuineCorrespondent:
    def test_a_no_reply_sender_is_never_genuine(self):
        assert is_genuine_correspondent("no_reply@email.apple.com") is False

    def test_the_user_is_never_his_own_correspondent(self):
        assert (
            is_genuine_correspondent(
                "psy.krsna@gmail.com", account_email="psykrsna@gmail.com"
            )
            is False
        )

    def test_a_real_person_is_genuine(self):
        assert is_genuine_correspondent("ada@example.com") is True

    def test_an_empty_address_is_not_genuine(self):
        assert is_genuine_correspondent("") is False


def _case(sender):
    """One archive decision from ``sender``, with a stale ever_replied claim."""
    items = [{"id": "t1", "from_email": sender, "from_domain": sender.split("@")[-1]}]
    decisions = [
        {"item_id": "t1", "proposed_action": "archive", "confidence": 0.9, "reasoning": "x"}
    ]
    stats = {sender: {"ever_replied": True, "replied_count": 24}}
    return decisions, stats, items


class TestGuardDefenceInDepth:
    """Layer 2: a stale ``sender_profiles`` row must not resurrect the defect."""

    def test_stale_self_address_claim_no_longer_holds_the_thread(self):
        decisions, stats, items = _case("psykrsna@gmail.com")

        out = apply_reply_history_guard(
            decisions, stats, items, account_email="psykrsna@gmail.com"
        )

        assert out[0]["proposed_action"] == "archive"
        assert out[0].get("decided_by") != "sender_history"

    def test_a_dotted_alias_spelling_is_also_ignored(self):
        decisions, stats, items = _case("psy.krsna@gmail.com")

        out = apply_reply_history_guard(
            decisions, stats, items, account_email="psykrsna@gmail.com"
        )

        assert out[0]["proposed_action"] == "archive"

    @pytest.mark.parametrize("sender", LIVE_NO_REPLY_SENDERS)
    def test_stale_no_reply_claim_no_longer_holds_the_thread(self, sender):
        decisions, stats, items = _case(sender)

        out = apply_reply_history_guard(decisions, stats, items)

        assert out[0]["proposed_action"] == "archive"

    def test_a_genuine_correspondent_is_still_held(self):
        decisions, stats, items = _case("ada@example.com")

        out = apply_reply_history_guard(
            decisions, stats, items, account_email="psykrsna@gmail.com"
        )

        assert out[0]["proposed_action"] == "keep"
        assert out[0]["decided_by"] == "sender_history"

    def test_an_explicit_override_still_wins_for_a_genuine_sender(self):
        decisions, stats, items = _case("ada@example.com")

        out = apply_reply_history_guard(
            decisions, stats, items, overrides={"ADA@example.com"}
        )

        assert out[0]["proposed_action"] == "archive"
