"""Concentration becomes tier-1 rules — through the EXISTING machinery.

This is the file that proves the phase's success measure is reachable rather
than aspirational. The fixture is the **measured** live distribution (do not
re-derive it): Facebook ~1,586 across five addresses, BookMyShow ~635 across two,
Jagriti Theatre 334, Apple ~176, PayPal 78, Twitter 40. All of it currently
collapses into "Notifications", which is exactly why 16 threads found no fitting
category and 46 landed under the bar.

**The seam is tested, not assumed.** Every mined rule is fed to
``tools.rules.apply_rules`` — the unchanged tier-1 matcher the unchanged
``graph.nodes.apply_deterministic_rules`` node calls — and the resulting decision
is asserted to be ``decided_by="rule"`` with the right category. A mined rule that
no matcher can fire is the signature defect of this codebase: plumbed, never
wired; green tests, absent feature. Deleting the ``mine_sender_rules`` body must
make these tests red, and it does.
"""

from __future__ import annotations

import pytest

from tools import rules as rules_tool
from tools import taxonomy_discovery as discovery
from tools.taxonomy import TaxonomyError

USER_ID = "test-mined-rules-user"

#: The measured sender concentration on the live account. Numbers taken from
#: spec/roadmap.md § "Sender concentration — the measured evidence".
MEASURED_SENDERS: dict[str, int] = {
    "notification@facebookmail.com": 503,
    "notification+kr4knbaqrsga@facebookmail.com": 489,
    "reminders@facebookmail.com": 250,
    "friendsuggestion@facebookmail.com": 184,
    "notification@priority.facebookmail.com": 130,
    "no-reply@entertainment.bookmyshow.com": 423,
    "no-reply@updates.bookmyshow.com": 212,
    "contact@jagrititheatre.com": 334,
    "no_reply@email.apple.com": 100,
    "noreply@email.apple.com": 76,
    "service@paypal.com": 78,
    "info@twitter.com": 40,
    # The genuine long tail: below the bar, so it stays the model's job.
    "occasional@smallsender.example": 4,
    "rare@tinysender.example": 1,
}

#: The categories a taxonomy derived from THIS inbox obviously wants.
PROPOSAL = [
    {
        "key": "social",
        "name": "Social",
        "description": "Facebook notifications, reminders and friend suggestions.",
        "default_action": "archive",
        "evidence_senders": [
            "notification@facebookmail.com",
            "notification+kr4knbaqrsga@facebookmail.com",
            "reminders@facebookmail.com",
            "friendsuggestion@facebookmail.com",
            "notification@priority.facebookmail.com",
        ],
    },
    {
        "key": "events_tickets",
        "name": "Events & Tickets",
        "description": "Ticketing and venue mail: bookings, showtimes, event reminders.",
        "default_action": "archive",
        "evidence_senders": [
            "no-reply@entertainment.bookmyshow.com",
            "no-reply@updates.bookmyshow.com",
            "contact@jagrititheatre.com",
        ],
    },
    {
        "key": "accounts_security",
        "name": "Accounts & Security",
        "description": "Apple account, sign-in and device notices.",
        "default_action": "archive",
        "evidence_senders": ["no_reply@email.apple.com", "noreply@email.apple.com"],
    },
    {
        "key": "billing",
        "name": "Billing & Subscriptions",
        "description": "Payment, invoice and subscription mail.",
        "default_action": "archive",
        "evidence_senders": ["service@paypal.com", "info@twitter.com"],
    },
]

#: Every sender at or above the bar. This is the set that MUST stop being asked
#: of the model — the single most important assertion in the phase.
CONCENTRATED = [s for s, n in MEASURED_SENDERS.items() if n >= discovery.CONCENTRATION_MIN_THREADS]


def _census() -> list[dict]:
    census: list[dict] = []
    domains: dict[str, int] = {}
    for sender, count in MEASURED_SENDERS.items():
        domain = sender.split("@")[-1]
        domains[domain] = domains.get(domain, 0) + count
        census.append(
            {
                "kind": "sender",
                "value": sender,
                "from_email": sender,
                "from_domain": domain,
                "list_id": None,
                "thread_count": count,
                "unread_count": count // 2,
                "ever_replied": False,
                "is_no_reply": "reply" in sender,
                "has_unsubscribe": True,
                "in_gap_set": False,
            }
        )
    for domain, count in domains.items():
        census.append(
            {
                "kind": "domain",
                "value": domain,
                "from_email": None,
                "from_domain": domain,
                "list_id": None,
                "thread_count": count,
                "unread_count": count // 2,
                "ever_replied": False,
                "is_no_reply": False,
                "has_unsubscribe": True,
                "in_gap_set": False,
            }
        )
    return census


def _item(sender: str, index: int = 0) -> dict:
    return {
        "id": f"item-{index}",
        "from_email": sender,
        "from_domain": sender.split("@")[-1],
        "list_id": None,
        "subject": "whatever",
    }


@pytest.fixture
def session(_isolated_db):
    from db.models import ChannelAccount, User
    from db.seed import ensure_default_taxonomy
    from db.session import create_db_session
    from tools.taxonomy import create_category

    with create_db_session() as s:
        s.add(User(id=USER_ID, email="mined@example.com", display_name="M"))
        s.add(
            ChannelAccount(
                id="test-mined-account",
                user_id=USER_ID,
                channel="gmail",
                account_email="mined@example.com",
                refresh_token_enc="enc",
                status="connected",
            )
        )
        ensure_default_taxonomy(s, USER_ID)
        for order, proposed in enumerate(PROPOSAL):
            create_category(
                s,
                USER_ID,
                key=proposed["key"],
                name=proposed["name"],
                description=proposed["description"],
                default_action=proposed["default_action"],
                sort_order=100 + order,
            )
        s.flush()
        yield s


class TestMinting:
    def test_every_sender_at_or_above_the_bar_gets_a_rule(self):
        minted = discovery.mine_sender_rules(_census(), PROPOSAL)
        matched = {r["matcher"].get("from_email") for r in minted}
        for sender in CONCENTRATED:
            assert sender in matched, f"{sender} was left for the LLM"

    def test_the_long_tail_stays_the_models_job(self):
        minted = discovery.mine_sender_rules(_census(), PROPOSAL)
        matched = {r["matcher"].get("from_email") for r in minted}
        assert "occasional@smallsender.example" not in matched
        assert "rare@tinysender.example" not in matched

    def test_mined_rules_carry_the_existing_enums_and_their_evidence(self):
        from domain.enums import RuleKind, RuleSource, RuleStatus

        for rule in discovery.mine_sender_rules(_census(), PROPOSAL):
            assert rule["kind"] == RuleKind.DETERMINISTIC
            assert rule["source"] == RuleSource.MINED
            assert rule["status"] == RuleStatus.ACTIVE
            # Well above the 0.75 confidence_floor AND the 0.80 autonomy bar, so
            # a mined decision reaches auto_act rather than below_threshold.
            assert rule["confidence"] > 0.80
            assert rule["evidence"]["thread_count"] >= discovery.CONCENTRATION_MIN_THREADS

    def test_a_sender_claimed_by_two_categories_has_no_dominant_category(self):
        contested = PROPOSAL + [
            {
                "key": "receipts",
                "name": "Receipts",
                "default_action": "archive",
                "evidence_senders": ["service@paypal.com"],
            }
        ]
        minted = discovery.mine_sender_rules(_census(), contested)
        matched = {r["matcher"].get("from_email") for r in minted}
        assert "service@paypal.com" not in matched


class TestTheSeam:
    """The mined rules are fed to the UNCHANGED tier-1 matcher, directly."""

    def test_apply_rules_resolves_every_concentrated_sender_by_rule(self):
        minted = discovery.mine_sender_rules(_census(), PROPOSAL)
        # Shaped exactly as graph.persistence.load_context loads them.
        loaded = [
            {
                "id": f"rule-{n}",
                "name": r["name"],
                "matcher": r["matcher"],
                "action": r["action"],
                "status": r["status"],
                "confidence": r["confidence"],
            }
            for n, r in enumerate(minted)
        ]
        items = [_item(sender, n) for n, sender in enumerate(CONCENTRATED)]

        decisions, unresolved = rules_tool.apply_rules(items, loaded)

        assert unresolved == [], [i["from_email"] for i in unresolved]
        assert len(decisions) == len(CONCENTRATED)
        for decision in decisions:
            assert decision["decided_by"] == "rule"
            assert decision["rule_id"] is not None
            assert decision["proposed_action"] == "archive"
            assert decision["confidence"] > 0.80

    def test_the_long_tail_falls_through_to_the_llm_queue(self):
        minted = discovery.mine_sender_rules(_census(), PROPOSAL)
        loaded = [
            {"id": f"rule-{n}", "matcher": r["matcher"], "action": r["action"],
             "status": r["status"], "confidence": r["confidence"], "name": r["name"]}
            for n, r in enumerate(minted)
        ]
        decisions, unresolved = rules_tool.apply_rules(
            [_item("occasional@smallsender.example")], loaded
        )
        assert decisions == []
        assert len(unresolved) == 1

    def test_facebook_lands_in_social_not_in_notifications(self):
        minted = discovery.mine_sender_rules(_census(), PROPOSAL)
        loaded = [
            {"id": f"rule-{n}", "matcher": r["matcher"], "action": r["action"],
             "status": r["status"], "confidence": r["confidence"], "name": r["name"]}
            for n, r in enumerate(minted)
        ]
        decisions, _ = rules_tool.apply_rules(
            [_item("notification@facebookmail.com"), _item("contact@jagrititheatre.com", 1)],
            loaded,
        )
        by_sender = {d["item_id"]: d for d in decisions}
        assert by_sender["item-0"]["category"] == "social"
        assert by_sender["item-1"]["category"] == "events_tickets"


class TestNeverArchiveGuard:
    def test_a_mined_archive_into_a_never_miss_category_is_refused_at_construction(self):
        bad = [
            {
                "key": "people",
                "name": "People",
                "default_action": "archive",
                "evidence_senders": ["notification@facebookmail.com"],
            }
        ]
        with pytest.raises(TaxonomyError):
            discovery.mine_sender_rules(_census(), bad)

    def test_it_is_rejected_at_materialisation_not_written_and_filtered(self, session):
        from db.models import Rule

        smuggled = [
            {
                "matcher": {"from_email": "notification@facebookmail.com"},
                "action": {"archive": True, "set_category": "people"},
                "category_key": "people",
                "confidence": 0.95,
                "kind": "deterministic",
                "source": "mined",
                "status": "active",
                "name": "smuggled",
            }
        ]
        with pytest.raises(TaxonomyError):
            discovery.materialise_rules(session, user_id=USER_ID, rules=smuggled)
        session.rollback()
        assert session.query(Rule).filter(Rule.user_id == USER_ID).count() == 0

    def test_a_whole_batch_is_refused_if_any_row_is_bad(self, session):
        from db.models import Rule

        good = discovery.mine_sender_rules(_census(), PROPOSAL)
        batch = good + [
            {
                "matcher": {"from_email": "x@y.example"},
                "action": {"archive": True, "set_category": "urgent"},
                "category_key": "urgent",
                "confidence": 0.95,
                "kind": "deterministic",
                "source": "mined",
                "status": "active",
                "name": "smuggled",
            }
        ]
        with pytest.raises(TaxonomyError):
            discovery.materialise_rules(session, user_id=USER_ID, rules=batch)
        session.rollback()
        # Not half-written: a partially applied re-discovery is not a state the
        # user can reason about or undo.
        assert session.query(Rule).filter(Rule.user_id == USER_ID).count() == 0


class TestMaterialisation:
    def test_rules_are_written_and_load_through_the_real_context_loader(self, session):
        from graph.persistence import load_context

        minted = discovery.mine_sender_rules(_census(), PROPOSAL)
        result = discovery.materialise_rules(session, user_id=USER_ID, rules=minted)
        assert result["created"] == len(minted)
        assert result["updated"] == 0
        session.commit()

        # The final seam: the rules the RUN will actually see. If mined rules do
        # not survive load_context they are invisible to the graph, which is the
        # "plumbed but never wired" failure this phase must not repeat.
        context = load_context(session, USER_ID)
        loaded = {r["matcher"].get("from_email") for r in context["rules"]}
        for sender in CONCENTRATED:
            assert sender in loaded, f"{sender} never reached the triage graph"

        decisions, unresolved = rules_tool.apply_rules(
            [_item(s, n) for n, s in enumerate(CONCENTRATED)], context["rules"]
        )
        assert unresolved == []
        assert all(d["decided_by"] == "rule" for d in decisions)

    def test_rerunning_discovery_updates_in_place_and_creates_no_duplicates(self, session):
        from db.models import Rule

        minted = discovery.mine_sender_rules(_census(), PROPOSAL)
        first = discovery.materialise_rules(session, user_id=USER_ID, rules=minted)
        session.commit()
        count_after_first = session.query(Rule).filter(Rule.user_id == USER_ID).count()

        second = discovery.materialise_rules(session, user_id=USER_ID, rules=minted)
        session.commit()

        assert second["created"] == 0
        assert second["updated"] == first["created"]
        assert session.query(Rule).filter(Rule.user_id == USER_ID).count() == count_after_first

    def test_a_user_rule_is_never_overwritten_by_a_mined_one(self, session):
        from db.models import Rule

        user_rule = Rule(
            user_id=USER_ID,
            name="my own facebook rule",
            kind="deterministic",
            source="user",
            matcher={"from_email": "notification@facebookmail.com"},
            action={"set_category": "people"},
            status="active",
            confidence=0.5,
        )
        session.add(user_rule)
        session.flush()
        before = (user_rule.name, dict(user_rule.matcher), dict(user_rule.action),
                  user_rule.confidence, user_rule.status, user_rule.source)

        minted = discovery.mine_sender_rules(_census(), PROPOSAL)
        result = discovery.materialise_rules(session, user_id=USER_ID, rules=minted)
        session.commit()

        assert result["skipped_user"] == 1
        refreshed = session.get(Rule, user_rule.id)
        assert (refreshed.name, dict(refreshed.matcher), dict(refreshed.action),
                refreshed.confidence, refreshed.status, refreshed.source) == before
        # And no shadow mined rule was minted alongside it — the first match wins
        # in apply_rules, so a second row for the same matcher is dead weight.
        same_matcher = [
            r
            for r in session.query(Rule).filter(Rule.user_id == USER_ID).all()
            if r.matcher.get("from_email") == "notification@facebookmail.com"
        ]
        assert len(same_matcher) == 1

    def test_a_rule_targeting_an_unapproved_category_is_refused(self, session):
        with pytest.raises(discovery.DiscoveryError):
            discovery.materialise_rules(
                session,
                user_id=USER_ID,
                rules=[
                    {
                        "matcher": {"from_email": "a@b.example"},
                        "action": {"archive": True, "set_category": "not_a_category"},
                        "category_key": "not_a_category",
                        "confidence": 0.95,
                        "kind": "deterministic",
                        "source": "mined",
                        "status": "active",
                        "name": "orphan",
                    }
                ],
            )
