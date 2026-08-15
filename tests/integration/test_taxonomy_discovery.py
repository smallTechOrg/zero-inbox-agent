"""The taxonomy is derived from THIS mailbox, and the concentration it finds
stops being asked of the model.

Real NVIDIA NIM via ``.env``. The census fixture replays the **measured** live
sender distribution (spec/roadmap.md § "Sender concentration — the measured
evidence"): Facebook ~1,586 across five addresses, BookMyShow ~635 across two,
Jagriti Theatre 334, Apple ~176, PayPal 78, Twitter 40. Those numbers are
measured, not re-derived, and nothing here touches the live database.

**The load-bearing assertion** is not "the buckets reached zero". It is *how*.
A run that reaches zero with ``notification@facebookmail.com`` still
``decided_by="llm"`` FAILS this file. ``counts.by_tier`` is the proof the
concentration was exploited deterministically, through the existing tier-1
matcher, rather than re-asked of the model in nicer clothes.

Deviation, deliberate and named: the *proposal* call is real (that is where the
model earns its keep), and the tier-1 resolution is real. The 365-row remainder
fixture's full end-to-end re-triage against real NIM is slice 3's
``test_inbox_zero_reframe.py``; here it is used for the **gap set**, which is what
discovery consumes it for.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from graph.runner import execute_triage
from tools import taxonomy_discovery as discovery

pytestmark = pytest.mark.integration

USER_ID = "test-taxdisc-user"
ACCOUNT_ID = "test-taxdisc-account"

#: Measured on the live account. Do not re-derive. Apple is split across its two
#: measured addresses to sum to ~176.
MEASURED: dict[str, int] = {
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
    "no-reply@accounts.google.com": 39,
    # The genuine long tail — below the bar, and it must STAY the model's job.
    "hello@tinystartup.example": 3,
    "maya@northwind.example": 2,
}

#: What the phase's single most important gate assertion is about.
CONCENTRATED = [s for s, n in MEASURED.items() if n >= discovery.CONCENTRATION_MIN_THREADS]

FACEBOOK = "notification@facebookmail.com"
BOOKMYSHOW = "no-reply@entertainment.bookmyshow.com"
JAGRITI = "contact@jagrititheatre.com"
APPLE = "no_reply@email.apple.com"


def _now():
    return datetime.now(timezone.utc)


def _item_row(index: int, sender: str) -> dict:
    """A normalised thread in the shape ``execute_triage(items=...)`` expects."""
    return {
        "id": f"tx-item-{index:05d}",
        "external_thread_id": f"tx-thread-{index:05d}",
        "external_message_ids": [f"tx-msg-{index:05d}"],
        "subject": f"Notice {index}",
        "from_name": sender.split("@")[0],
        "from_email": sender,
        "from_domain": sender.split("@")[-1],
        "to_emails": ["taxdisc@example.com"],
        "cc_emails": [],
        "list_id": None,
        "unsubscribe_url": f"https://{sender.split('@')[-1]}/unsub",
        "message_count": 1,
        "has_attachments": False,
        "snippet": "Routine automated notice. " * 4,
        "internal_date": _now() - timedelta(days=index % 30),
        "is_unread": index % 2 == 0,
        "channel_labels": ["INBOX"],
    }


@pytest.fixture(scope="module")
def _fixture_rows() -> list[dict]:
    rows: list[dict] = []
    index = 0
    for sender, count in MEASURED.items():
        for _ in range(count):
            rows.append(_item_row(index, sender))
            index += 1
    return rows


@pytest.fixture
def session(_isolated_db, _fixture_rows):
    """A seeded mailbox: user, account, the seed taxonomy and the measured items."""
    from db.models import ChannelAccount, Item, User, UserSettings
    from db.seed import ensure_default_taxonomy
    from db.session import create_db_session

    with create_db_session() as s:
        s.add(User(id=USER_ID, email="taxdisc@example.com", display_name="Tax"))
        s.add(
            ChannelAccount(
                id=ACCOUNT_ID,
                user_id=USER_ID,
                channel="gmail",
                account_email="taxdisc@example.com",
                refresh_token_enc="enc",
                status="connected",
            )
        )
        s.add(UserSettings(user_id=USER_ID, confidence_floor=0.75, dry_run=True))
        ensure_default_taxonomy(s, USER_ID)
        for row in _fixture_rows:
            s.add(
                Item(
                    id=row["id"],
                    user_id=USER_ID,
                    channel_account_id=ACCOUNT_ID,
                    external_thread_id=row["external_thread_id"],
                    subject=row["subject"],
                    from_email=row["from_email"],
                    from_domain=row["from_domain"],
                    unsubscribe_url=row["unsubscribe_url"],
                    is_unread=row["is_unread"],
                    internal_date=row["internal_date"],
                )
            )
        s.commit()
        yield s


# --------------------------------------------------------------- the census


class TestCensusOnTheMeasuredDistribution:
    def test_every_measured_sender_appears_with_its_measured_count(self, session):
        census = {
            r["value"]: r
            for r in discovery.build_census(session, user_id=USER_ID)
            if r["kind"] == "sender"
        }
        for sender, count in MEASURED.items():
            assert census[sender]["thread_count"] == count, sender

    def test_the_domain_families_are_visible_as_families(self, session):
        census = {
            r["value"]: r
            for r in discovery.build_census(session, user_id=USER_ID)
            if r["kind"] == "domain"
        }
        # ~1,586 Facebook threads across five addresses is exactly the
        # concentration the generic "Notifications" bucket was hiding.
        assert census["facebookmail.com"]["thread_count"] == 1426
        assert census["priority.facebookmail.com"]["thread_count"] == 130
        assert census["jagrititheatre.com"]["thread_count"] == 334


# ------------------------------------------------------ the real proposal


@pytest.fixture(scope="module")
def _proposal_cache() -> dict:
    return {}


#: Threads per concentrated sender fed to the triage run. The CENSUS keeps the
#: full measured counts — that is what the >= 10 concentration bar is computed
#: from, and sampling it would change the answer. The RUN's assertion is
#: per-sender ("this sender is decided by rule, never by the model"), so a fixed
#: slice per sender proves it exactly as well as 2,800 rows would, and keeps the
#: gate to seconds rather than tens of minutes.
RUN_SAMPLE_PER_SENDER = 12


def _run_sample(rows: list[dict]) -> list[dict]:
    taken: dict[str, int] = {}
    out: list[dict] = []
    for row in rows:
        sender = row["from_email"]
        if sender not in CONCENTRATED:
            continue
        if taken.get(sender, 0) >= RUN_SAMPLE_PER_SENDER:
            continue
        taken[sender] = taken.get(sender, 0) + 1
        out.append(row)
    assert set(taken) == set(CONCENTRATED), "every concentrated sender must be exercised"
    return out


class TestRealProposal:
    """One real NIM call. The model names and groups; the census decides what exists."""

    @pytest.fixture
    def proposal(self, session, _require_llm_key, _proposal_cache):
        # ONE real call for the whole class. The assertions below interrogate a
        # single real answer from different angles; re-asking per assertion would
        # cost a minute each and, worse, let one assertion pass against an answer
        # a sibling assertion never saw.
        if "result" not in _proposal_cache:
            census = discovery.build_census(session, user_id=USER_ID)
            _proposal_cache["result"] = discovery.propose_taxonomy(
                session, user_id=USER_ID, census=census, gap_set=[]
            )
        return _proposal_cache["result"]

    def test_the_model_answers_and_the_proposal_is_not_partial(self, proposal):
        assert proposal["partial"] is False, proposal["partial_reason"]
        assert proposal["proposal"], "the model returned no categories at all"

    def test_every_category_names_at_least_one_real_sender(self, proposal, session):
        census_values = {r["value"] for r in discovery.build_census(session, user_id=USER_ID)}
        for category in proposal["proposal"]:
            if category.get("retained_safety"):
                continue
            assert category["evidence_senders"], category["key"]
            for value in category["evidence_senders"]:
                assert value in census_values, f"{value} is not in the census"

    def test_the_users_dominant_senders_are_named_by_name(self, proposal):
        """Apple, Facebook, PayPal and BookMyShow appear — not a generic list."""
        named = {v for c in proposal["proposal"] for v in c["evidence_senders"]}
        for sender in (FACEBOOK, BOOKMYSHOW, JAGRITI, APPLE, "service@paypal.com"):
            assert sender in named, f"{sender} was not placed in any category"

    def test_every_sender_above_the_bar_is_placed(self, proposal):
        assert proposal["coverage"]["uncovered_concentrated_senders"] == []

    def test_never_miss_categories_survive_and_stay_keep(self, proposal):
        from tools.taxonomy import NEVER_ARCHIVE_KEYS

        by_key = {c["key"]: c for c in proposal["proposal"]}
        for key in ("people", "urgent", "important"):
            assert key in by_key, f"{key} was retired by discovery"
        for key in by_key:
            if key in NEVER_ARCHIVE_KEYS:
                assert by_key[key]["default_action"] != "archive"


# ------------------------------------- concentration -> tier 1 -> the run
#
# This is the section the phase's success measure rests on.


def _approve(session, proposal: list[dict]) -> dict:
    """Approve a proposal the way ``POST /api/taxonomy/apply`` does."""
    from sqlalchemy import select

    from db.models import Category
    from tools.taxonomy import create_category, update_category

    existing = {
        row.key: row
        for row in session.execute(
            select(Category).where(Category.user_id == USER_ID)
        ).scalars()
    }
    for order, category in enumerate(proposal):
        if category["key"] in existing:
            update_category(
                session,
                USER_ID,
                existing[category["key"]].id,
                name=category["name"],
                description=category["description"],
                default_action=category["default_action"],
            )
        else:
            create_category(
                session,
                USER_ID,
                key=category["key"],
                name=category["name"],
                description=category["description"],
                default_action=category["default_action"],
                sort_order=100 + order,
            )
    session.flush()
    census = discovery.build_census(session, user_id=USER_ID)
    rules = discovery.mine_sender_rules(census, proposal)
    minted = discovery.materialise_rules(session, user_id=USER_ID, rules=rules)
    session.commit()
    return {"rules": rules, "minted": minted}


#: A deterministic stand-in for the model's grouping, used by the tier-1
#: assertions so THEY are testing the tier-1 seam rather than re-testing the
#: model's naming (which the real-NIM section above already covers). The
#: evidence lists are the measured senders, unchanged.
GROUPING = [
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
        "name": "Events and Tickets",
        "description": "Ticketing, showtimes and venue mail.",
        "default_action": "archive",
        "evidence_senders": [
            "no-reply@entertainment.bookmyshow.com",
            "no-reply@updates.bookmyshow.com",
            "contact@jagrititheatre.com",
        ],
    },
    {
        "key": "accounts_security",
        "name": "Accounts and Security",
        "description": "Apple and Google account, sign-in and device notices.",
        "default_action": "archive",
        "evidence_senders": [
            "no_reply@email.apple.com",
            "noreply@email.apple.com",
            "no-reply@accounts.google.com",
        ],
    },
    {
        "key": "billing",
        "name": "Billing and Subscriptions",
        "description": "Payment, invoice and subscription mail.",
        "default_action": "archive",
        "evidence_senders": ["service@paypal.com", "info@twitter.com"],
    },
]


class TestConcentrationBecomesTierOne:
    @pytest.fixture
    def approved(self, session):
        return _approve(session, GROUPING)

    def test_a_mined_rule_exists_for_every_sender_above_the_bar(self, approved):
        matched = {r["matcher"].get("from_email") for r in approved["rules"]}
        for sender in CONCENTRATED:
            assert sender in matched, f"{sender} would still be asked of the model"
        assert approved["minted"]["created"] == len(approved["rules"])

    def test_the_run_resolves_them_by_rule_and_sends_zero_of_them_to_the_llm(
        self, session, approved, _fixture_rows
    ):
        """THE gate assertion. Zero reached with `decided_by="llm"` is a failure."""
        concentrated_items = _run_sample(_fixture_rows)
        state = execute_triage(
            user_id=USER_ID,
            channel_account_id=ACCOUNT_ID,
            items=concentrated_items,
            dry_run=True,
        )

        counts = state.get("counts") or {}
        by_tier = counts.get("by_tier") or {}

        # THE assertion. Not one concentrated thread was classified by the model.
        assert by_tier.get("llm", 0) == 0, by_tier
        assert by_tier.get("sender_history", 0) == 0, by_tier
        # The classification tiers below tier 1 were never entered at all — this
        # is what "sends zero of them to the LLM" means concretely, and it is
        # stronger than counting tokens.
        assert state.get("llm_decisions") == [], state.get("llm_decisions")
        assert state.get("deep_queue") == [], state.get("deep_queue")

        # `by_tier` may also carry `reviewer` rows: the never-miss second pass
        # audits tier-1 archives and re-stamps the ones it vetoes. That is a
        # SAFETY layer on top of tier 1, not a second classifier — the row keeps
        # its `rule_id`, asserted below — so it is counted, not assumed away.
        assert by_tier.get("rule", 0) >= 0.9 * len(concentrated_items), by_tier
        assert sum(by_tier.values()) == len(concentrated_items), by_tier

        from db.models import Decision
        from db.session import create_db_session

        with create_db_session() as check:
            rows = (
                check.query(Decision)
                .filter(Decision.run_id == state["run_id"])
                .all()
            )
            assert len(rows) == len(concentrated_items)
            for row in rows:
                # Every single row was DECIDED by a mined tier-1 rule. A reviewer
                # veto changes the action, never the origin: `rule_id` survives,
                # so a row that reached the model for classification would show
                # up here as a null and fail.
                assert row.rule_id is not None, (row.decided_by, row.reasoning[:120])
                assert row.decided_by in ("rule", "reviewer"), row.decided_by
                # Above the 0.75 floor AND the 0.80 autonomy bar — which is why
                # below_threshold goes to zero by construction, not by hope.
                assert row.confidence > 0.80
                assert row.category_id is not None

    def test_the_genuine_long_tail_still_reaches_the_model(
        self, session, approved, _fixture_rows, _require_llm_key
    ):
        """The LLM is reserved for the tail — not eliminated, redirected."""
        tail = [
            row
            for row in _fixture_rows
            if row["from_email"] in ("hello@tinystartup.example", "maya@northwind.example")
        ]
        assert tail
        state = execute_triage(
            user_id=USER_ID,
            channel_account_id=ACCOUNT_ID,
            items=tail,
            dry_run=True,
        )
        by_tier = (state.get("counts") or {}).get("by_tier") or {}
        assert by_tier.get("rule", 0) == 0, by_tier
        assert sum(by_tier.values()) == len(tail), by_tier

    def test_rediscovery_is_idempotent_and_mints_no_duplicates(self, session, approved):
        from db.models import Rule

        before = session.query(Rule).filter(Rule.user_id == USER_ID).count()
        again = discovery.materialise_rules(
            session, user_id=USER_ID, rules=approved["rules"]
        )
        session.commit()
        assert again["created"] == 0
        assert session.query(Rule).filter(Rule.user_id == USER_ID).count() == before


# ----------------------------------------------------- the gap buckets


def _remainder_365() -> tuple[list[dict], list[dict]]:
    """Slice 3's 365-row remainder fixture (227/76/46/16/0), imported not copied.

    Fails LOUDLY (never skips) if the fixture is absent: a silently-skipped
    cross-slice seam is precisely the "plumbed but never wired" defect this phase
    exists to stop repeating.
    """
    from tests.fixtures.phase9 import remainder_365 as fixture

    items, decisions = fixture.build_remainder()
    return items, decisions


class TestTheGapBucketsGoToZero:
    """`needs_your_call` and `below_threshold` are the feedback signal."""

    def _seed_remainder(self, session) -> int:
        """Persist the 365 rows with the buckets the REAL policy derives.

        The fixture deliberately does not assert its own buckets — they fall out
        of ``apply_confidence_floor`` + ``classify_autonomy_state``. So the same
        policy is run here and its verdict is written to ``autonomy_state``,
        exactly as ``persist_run_results`` does in a real run. Anything else
        would be this test inventing the distribution it then measures.
        """
        from tests.fixtures.phase9 import remainder_365 as fixture

        from db.models import Decision, Item, TriageRun
        from graph.autonomy import classify_autonomy_state
        from tools.never_miss import apply_confidence_floor

        items, decisions = _remainder_365()
        assert len(items) == 365 and len(decisions) == 365

        categories = fixture.seed_category_index()
        items_by_id = {i["id"]: i for i in items}
        floored = apply_confidence_floor(decisions, fixture.CONFIDENCE_FLOOR)

        session.add(
            TriageRun(id="run-remainder", user_id=USER_ID, channel_account_id=ACCOUNT_ID)
        )
        for item in items:
            session.add(
                Item(
                    id=item["id"],
                    user_id=USER_ID,
                    channel_account_id=ACCOUNT_ID,
                    external_thread_id=item["external_thread_id"],
                    subject=item["subject"],
                    from_email=item["from_email"],
                    from_domain=item["from_email"].split("@")[-1],
                    internal_date=_now(),
                )
            )
        session.flush()

        gap = 0
        for index, decision in enumerate(floored):
            state = classify_autonomy_state(
                decision,
                categories.get(decision.get("category")),
                fixture.SETTINGS,
                fixture.fixed_sender_stats(),
                {},
                item=items_by_id.get(decision["item_id"]),
            )
            session.add(
                Decision(
                    id=f"rem-dec-{index:04d}",
                    user_id=USER_ID,
                    item_id=decision["item_id"],
                    run_id="run-remainder",
                    status=decision.get("status") or "proposed",
                    autonomy_state=state,
                    proposed_action=decision.get("proposed_action") or "keep",
                    decided_by=decision.get("decided_by") or "llm",
                    confidence=float(decision.get("confidence") or 0.0),
                    reasoning=decision.get("reasoning") or "",
                    time_sensitive=bool(decision.get("time_sensitive")),
                )
            )
            if state in discovery.GAP_STATUSES:
                gap += 1
        session.commit()
        return gap

    def test_the_gap_set_finds_the_measured_16_and_46(self, session):
        """Both buckets, from both columns.

        ``needs_your_call`` lands in ``Decision.status``; ``below_threshold``
        lands ONLY in ``autonomy_state``. A gap set read from ``status`` alone
        returns 16 and looks plausible while missing all 46 — the taxonomy would
        then be derived against a hole three times smaller than the real one.
        """
        from tests.fixtures.phase9.remainder_365 import MEASURED_DISTRIBUTION

        expected = (
            MEASURED_DISTRIBUTION["needs_your_call"]
            + MEASURED_DISTRIBUTION["below_threshold"]
        )
        assert expected == 62, MEASURED_DISTRIBUTION

        seeded_gap = self._seed_remainder(session)
        assert seeded_gap == expected, seeded_gap

        gap_set = discovery.build_gap_set(session, user_id=USER_ID)
        assert len(gap_set) == expected
        buckets = {g["status"] for g in gap_set}
        assert buckets == set(discovery.GAP_STATUSES), buckets
        for entry in gap_set:
            assert len(entry["subject"]) <= discovery.GAP_SUBJECT_MAX_CHARS

    def test_the_gap_senders_are_flagged_in_the_census(self, session):
        self._seed_remainder(session)
        census = discovery.build_census(session, user_id=USER_ID)
        flagged = {r["value"] for r in census if r["kind"] == "sender" and r["in_gap_set"]}
        assert flagged, "no census row was flagged as producing a gap thread"

    def test_under_the_derived_taxonomy_both_buckets_are_zero_for_the_concentration(
        self, session, _fixture_rows
    ):
        """A concentrated thread can no longer land in either gap bucket.

        Under the seed taxonomy these threads produced the 16 `needs_your_call`
        and a large share of the 46 `below_threshold`, because the model was
        being asked to squeeze them into "Notifications". Under the derived
        taxonomy they are never asked at all.
        """
        from graph.autonomy import classify_autonomy_state

        _approve(session, GROUPING)
        concentrated_items = _run_sample(_fixture_rows)
        state = execute_triage(
            user_id=USER_ID,
            channel_account_id=ACCOUNT_ID,
            items=concentrated_items,
            dry_run=True,
        )

        from db.models import Category, Decision
        from db.session import create_db_session

        with create_db_session() as check:
            categories = {
                c.id: {
                    "key": c.key,
                    "default_action": c.default_action,
                    "auto_act_threshold": c.auto_act_threshold,
                }
                for c in check.query(Category).filter(Category.user_id == USER_ID).all()
            }
            states: dict[str, int] = {}
            for row in check.query(Decision).filter(
                Decision.run_id == state["run_id"]
            ).all():
                bucket = classify_autonomy_state(
                    {
                        "proposed_action": row.proposed_action,
                        "confidence": row.confidence,
                        "status": row.status,
                        "decided_by": row.decided_by,
                        "time_sensitive": row.time_sensitive,
                    },
                    categories.get(row.category_id),
                    {"auto_act_threshold": 0.80, "confidence_floor": 0.75},
                    {},
                    {},
                    item={"from_email": "x@y.example"},
                )
                states[bucket] = states.get(bucket, 0) + 1

        # The two gap buckets — 16 and 46 under the seed taxonomy — are zero, by
        # construction rather than by hope: the mail that produced them is no
        # longer being asked of the model.
        assert states.get("needs_your_call", 0) == 0, states
        assert states.get("below_threshold", 0) == 0, states
        # The rest is acted on, save whatever the never-miss reviewer held back —
        # which is the safety layer doing its job, not a gap.
        assert states.get("auto_act", 0) >= 0.9 * len(concentrated_items), states
        assert sum(states.values()) == len(concentrated_items), states


# ------------------------------------------------- GET /api/categories/{id}/usage


class TestCategoryUsageEndpoint:
    @pytest.fixture
    def client(self, session, monkeypatch):
        from fastapi.testclient import TestClient

        from api import app, session as api_session

        app.dependency_overrides[api_session.require_user_id] = lambda: USER_ID
        try:
            with TestClient(app) as c:
                yield c
        finally:
            app.dependency_overrides.clear()

    def test_an_unused_category_reports_zero_and_is_safe_to_delete(self, session, client):
        from tools.taxonomy import create_category

        category = create_category(
            session, USER_ID, key="e2e-actions-test", name="E2EActionsTest"
        )
        session.commit()

        body = client.get(f"/api/categories/{category.id}/usage").json()
        assert body["error"] is None
        assert body["data"]["decisions"] == 0
        assert body["data"]["rules"] == 0
        assert body["data"]["items"] == 0
        assert body["data"]["safe_to_delete"] is True

        deleted = client.delete(f"/api/categories/{category.id}")
        assert deleted.status_code == 200
        assert deleted.json()["data"]["deleted"] is True

    def test_a_referenced_category_reports_true_counts_and_refuses_deletion(
        self, session, client
    ):
        from db.models import Category, Decision, Item, TriageRun

        category = session.query(Category).filter(
            Category.user_id == USER_ID, Category.key == "newsletters"
        ).one()
        item_id = next(
            i.id for i in session.query(Item).filter(Item.user_id == USER_ID).limit(1)
        )
        # Two runs over the SAME thread — i.e. the thread was re-triaged, which is
        # ordinary. Two decisions, one thread: the two counts genuinely differ,
        # which is exactly why the contract carries both.
        for n in range(2):
            session.add(
                TriageRun(
                    id=f"run-usage-{n}", user_id=USER_ID, channel_account_id=ACCOUNT_ID
                )
            )
            session.flush()
            session.add(
                Decision(
                    id=f"usage-dec-{n}",
                    user_id=USER_ID,
                    item_id=item_id,
                    run_id=f"run-usage-{n}",
                    category_id=category.id,
                    decided_by="llm",
                    confidence=0.9,
                )
            )
        session.commit()

        data = client.get(f"/api/categories/{category.id}/usage").json()["data"]
        assert data["decisions"] == 2
        assert data["items"] == 1
        assert data["safe_to_delete"] is False

        refused = client.delete(f"/api/categories/{category.id}")
        assert refused.status_code == 422
        assert "Nothing was deleted" in refused.json()["error"]["message"]
        # And it is genuinely still there.
        assert session.get(Category, category.id) is not None
