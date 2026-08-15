"""Discovery is evidence-first, model-second — and it never lies about which.

Three things are asserted here and each one is a defect that would be invisible
in production:

1. The census is **correct and free** — right counts, right signals, and no
   subjects anywhere in the aggregates.
2. A category with **no evidence** never reaches the user. That is the model
   pattern-matching on generic inbox advice, which is the exact failure discovery
   exists to end.
3. With the model dead, discovery returns the deterministic proposal **marked
   partial**, and **never** the seed six presented as derived. A generic list
   labelled "derived from your mail" is a lie the user cannot detect.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tools import taxonomy_discovery as discovery
from tools.rules import DEFAULT_CATEGORY_KEYS

USER_ID = "test-discovery-user"
ACCOUNT_ID = "test-discovery-account"


def _now():
    return datetime.now(timezone.utc)


def _seed(session, senders: dict[str, int], *, unread: set[str] = frozenset()):
    """``{from_email: thread_count}`` -> real ``Item`` rows."""
    from db.models import ChannelAccount, Item, User

    session.add(User(id=USER_ID, email="discovery@example.com", display_name="D"))
    session.add(
        ChannelAccount(
            id=ACCOUNT_ID,
            user_id=USER_ID,
            channel="gmail",
            account_email="discovery@example.com",
            refresh_token_enc="enc",
            status="connected",
        )
    )
    index = 0
    for sender, count in senders.items():
        for _ in range(count):
            session.add(
                Item(
                    id=f"item-{index:05d}",
                    user_id=USER_ID,
                    channel_account_id=ACCOUNT_ID,
                    external_thread_id=f"thread-{index:05d}",
                    subject=f"Subject {index}",
                    from_email=sender,
                    from_domain=sender.split("@")[-1],
                    unsubscribe_url=(
                        f"https://{sender.split('@')[-1]}/unsub" if "news" in sender else None
                    ),
                    is_unread=sender in unread,
                    internal_date=_now() - timedelta(days=index % 30),
                )
            )
            index += 1
    session.flush()


@pytest.fixture
def session(_isolated_db):
    from db.session import create_db_session

    with create_db_session() as s:
        yield s


class TestCensus:
    def test_counts_threads_by_sender_domain_and_list(self, session):
        _seed(
            session,
            {
                "notification@facebookmail.com": 12,
                "reminders@facebookmail.com": 7,
                "maya@northwind.example": 3,
            },
            unread={"notification@facebookmail.com"},
        )
        census = discovery.build_census(session, user_id=USER_ID)
        rows = {(r["kind"], r["value"]): r for r in census}

        assert rows[("sender", "notification@facebookmail.com")]["thread_count"] == 12
        assert rows[("sender", "reminders@facebookmail.com")]["thread_count"] == 7
        # The domain aggregate is the SUM of its senders — if these two ever
        # disagree the user sees a wrong thread count on the approval screen.
        assert rows[("domain", "facebookmail.com")]["thread_count"] == 19
        assert rows[("sender", "notification@facebookmail.com")]["unread_count"] == 12
        assert rows[("sender", "reminders@facebookmail.com")]["unread_count"] == 0

    def test_is_no_reply_is_computed_for_every_row(self, session):
        _seed(
            session,
            {
                "no_reply@email.apple.com": 4,
                "noreply@email.apple.com": 3,
                "no-reply@entertainment.bookmyshow.com": 5,
                "donotreply@example.com": 2,
                "do-not-reply@example.com": 2,
                "maya@northwind.example": 2,
            },
        )
        census = {r["value"]: r for r in discovery.build_census(session, user_id=USER_ID)}
        for address in (
            "no_reply@email.apple.com",
            "noreply@email.apple.com",
            "no-reply@entertainment.bookmyshow.com",
            "donotreply@example.com",
            "do-not-reply@example.com",
        ):
            assert census[address]["is_no_reply"] is True, address
        assert census["maya@northwind.example"]["is_no_reply"] is False

    def test_the_census_carries_no_subjects(self, session):
        """The privacy invariant: aggregates are counts and addresses only."""
        _seed(session, {"bulk@news.example": 5})
        census = discovery.build_census(session, user_id=USER_ID)
        assert census
        for row in census:
            assert "subject" not in row
            blob = repr(row)
            assert "Subject " not in blob

    def test_ever_replied_comes_from_sender_profiles_and_ignores_no_reply(self, session):
        from db.models import SenderProfile

        _seed(session, {"maya@northwind.example": 3, "no_reply@email.apple.com": 3})
        session.add(
            SenderProfile(
                user_id=USER_ID,
                sender_email="maya@northwind.example",
                sender_domain="northwind.example",
                ever_replied=True,
                replied_count=4,
            )
        )
        # A stale profile asserting a reply to an address that cannot receive one
        # is exactly the live "24 replies of 0 received" signature. Defence in
        # depth: the census refuses the claim even if the row survives.
        session.add(
            SenderProfile(
                user_id=USER_ID,
                sender_email="no_reply@email.apple.com",
                sender_domain="email.apple.com",
                ever_replied=True,
                replied_count=24,
            )
        )
        session.flush()

        census = {r["value"]: r for r in discovery.build_census(session, user_id=USER_ID)}
        assert census["maya@northwind.example"]["ever_replied"] is True
        assert census["no_reply@email.apple.com"]["ever_replied"] is False

    def test_gap_set_senders_are_flagged_and_subjects_truncated(self, session):
        from db.models import Category, Decision, TriageRun

        _seed(session, {"weird@unplaceable.example": 4})
        session.add(
            Category(
                id="cat-x", user_id=USER_ID, key="notifications", name="Notifications"
            )
        )
        session.add(
            TriageRun(id="run-1", user_id=USER_ID, channel_account_id=ACCOUNT_ID)
        )
        session.flush()
        session.add(
            Decision(
                id="dec-1",
                user_id=USER_ID,
                item_id="item-00000",
                run_id="run-1",
                status="needs_your_call",
                decided_by="llm",
                confidence=0.4,
                reasoning="no category fits",
            )
        )
        session.flush()

        census = {r["value"]: r for r in discovery.build_census(session, user_id=USER_ID)}
        assert census["weird@unplaceable.example"]["in_gap_set"] is True

        gap = discovery.build_gap_set(session, user_id=USER_ID)
        assert len(gap) == 1
        assert gap[0]["status"] == "needs_your_call"
        assert len(gap[0]["subject"]) <= discovery.GAP_SUBJECT_MAX_CHARS


class TestValidation:
    def _census(self):
        return [
            {
                "kind": "sender",
                "value": "notification@facebookmail.com",
                "from_email": "notification@facebookmail.com",
                "from_domain": "facebookmail.com",
                "list_id": None,
                "thread_count": 503,
                "unread_count": 100,
                "ever_replied": False,
                "is_no_reply": False,
                "has_unsubscribe": True,
                "in_gap_set": True,
            }
        ]

    def test_an_evidence_free_category_is_rejected(self):
        kept = discovery.validate_proposal(
            [
                {
                    "key": "social",
                    "name": "Social",
                    "default_action": "archive",
                    "evidence_senders": ["notification@facebookmail.com"],
                },
                {
                    "key": "productivity",
                    "name": "Productivity",
                    "default_action": "archive",
                    "evidence_senders": [],
                },
            ],
            self._census(),
        )
        assert [c["key"] for c in kept] == ["social"]

    def test_a_hallucinated_sender_is_dropped(self):
        kept = discovery.validate_proposal(
            [
                {
                    "key": "social",
                    "name": "Social",
                    "default_action": "archive",
                    "evidence_senders": [
                        "notification@facebookmail.com",
                        "invented@nowhere.example",
                    ],
                }
            ],
            self._census(),
        )
        assert kept[0]["evidence_senders"] == ["notification@facebookmail.com"]

    def test_a_never_archive_key_can_never_be_proposed_as_archive(self):
        kept = discovery.validate_proposal(
            [
                {
                    "key": "people",
                    "name": "People",
                    "default_action": "archive",
                    "evidence_senders": ["notification@facebookmail.com"],
                }
            ],
            self._census(),
        )
        assert kept[0]["default_action"] == "keep"

    def test_a_sender_claimed_twice_is_claimed_once(self):
        kept = discovery.validate_proposal(
            [
                {
                    "key": "social",
                    "name": "Social",
                    "default_action": "archive",
                    "evidence_senders": ["notification@facebookmail.com"],
                },
                {
                    "key": "notifications",
                    "name": "Notifications",
                    "default_action": "archive",
                    "evidence_senders": ["notification@facebookmail.com"],
                },
            ],
            self._census(),
        )
        assert [c["key"] for c in kept] == ["social"]


class TestModelFailureFallback:
    def test_a_dead_model_returns_a_derived_proposal_marked_partial(
        self, session, monkeypatch
    ):
        """Never the seed six dressed up as discovery."""
        from db.seed import ensure_default_taxonomy

        _seed(
            session,
            {
                "notification@facebookmail.com": 503,
                "reminders@facebookmail.com": 250,
                "no-reply@entertainment.bookmyshow.com": 423,
                "contact@jagrititheatre.com": 334,
                "no_reply@email.apple.com": 44,
            },
        )
        ensure_default_taxonomy(session, USER_ID)
        session.flush()

        class DeadClient:
            def call_model_sync(self, *a, **k):
                raise RuntimeError("all providers exhausted")

        monkeypatch.setattr("llm.client.get_llm_client", lambda: DeadClient())

        census = discovery.build_census(session, user_id=USER_ID)
        result = discovery.propose_taxonomy(
            session, user_id=USER_ID, census=census, gap_set=[]
        )

        assert result["partial"] is True
        assert result["partial_reason"]
        assert "model was unavailable" in result["partial_reason"]

        keys = {c["key"] for c in result["proposal"]}
        # The whole point: the fallback is still DERIVED. The user's real senders
        # appear by name; it is not the generic default list relabelled.
        assert "facebook" in keys
        assert "bookmyshow" in keys or "book My Show".lower().replace(" ", "") in keys
        assert not keys.issubset(set(DEFAULT_CATEGORY_KEYS))

        for category in result["proposal"]:
            assert category["evidence_senders"] or category.get("retained_safety"), category

    def test_never_miss_categories_are_never_retired_by_omission(
        self, session, monkeypatch
    ):
        from db.seed import ensure_default_taxonomy

        _seed(session, {"notification@facebookmail.com": 503})
        ensure_default_taxonomy(session, USER_ID)
        session.flush()

        class TerseClient:
            """A model that proposes exactly one category and forgets the rest."""

            def call_model_sync(self, *a, **k):
                class R:
                    text = (
                        '[{"key":"social","name":"Social","description":"fb",'
                        '"default_action":"archive","rationale":"volume",'
                        '"evidence_senders":["notification@facebookmail.com"]}]'
                    )

                return R()

        monkeypatch.setattr("llm.client.get_llm_client", lambda: TerseClient())

        census = discovery.build_census(session, user_id=USER_ID)
        result = discovery.propose_taxonomy(
            session, user_id=USER_ID, census=census, gap_set=[]
        )
        keys = {c["key"] for c in result["proposal"]}
        assert {"people", "urgent", "important"} <= keys
        assert result["partial"] is False
        for key in ("people", "urgent", "important"):
            assert next(c for c in result["proposal"] if c["key"] == key)[
                "default_action"
            ] == "keep"


class TestCoverage:
    def test_coverage_names_the_concentrated_senders_it_failed_to_place(self, session):
        _seed(
            session,
            {"notification@facebookmail.com": 503, "unplaced@bigsender.example": 120},
        )
        census = discovery.build_census(session, user_id=USER_ID)
        coverage = discovery._coverage(
            [
                {
                    "key": "social",
                    "evidence_senders": ["notification@facebookmail.com"],
                }
            ],
            census,
            [],
        )
        assert coverage["covered_threads"] == 503
        assert coverage["uncovered_threads"] == 120
        # Named, never rounded away.
        assert coverage["uncovered_concentrated_senders"] == ["unplaced@bigsender.example"]


def test_important_is_in_the_seed_set_and_can_never_be_archive():
    from tools.taxonomy import NEVER_ARCHIVE_KEYS, TaxonomyError, validate_default_action

    assert "important" in DEFAULT_CATEGORY_KEYS
    assert "important" in NEVER_ARCHIVE_KEYS
    with pytest.raises(TaxonomyError):
        validate_default_action("important", "archive")
    validate_default_action("important", "keep")
