"""Graph assembly + the zero-token path (tiers 1-2 resolve everything)."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from graph.agent import build_triage_graph, triage_graph
from graph.runner import execute_triage, run_triage
from graph.persistence import persist_sender_profiles

USER = "user-1"
ACCOUNT = "acct-1"


def item(index, **overrides):
    base = {
        "id": f"i{index}",
        "external_thread_id": f"t{index}",
        "subject": f"Weekly digest {index}",
        "from_name": "Substack",
        "from_email": "news@substack.com",
        "from_domain": "substack.com",
        "list_id": "<weekly.substack.com>",
        "snippet_redacted": "Top stories this week",
        "message_count": 1,
        "has_attachments": False,
        "is_unread": True,
        "internal_date": datetime.now(timezone.utc),
    }
    base.update(overrides)
    return base


@pytest.fixture
def seeded(_isolated_db):
    """A user whose single active rule matches every thread in the run."""
    from db.models import ChannelAccount, Category, Rule, User, UserSettings
    from db.session import create_db_session
    from tools.rules import DEFAULT_TAXONOMY

    with create_db_session() as session:
        session.add(User(id=USER, email="u@example.com", display_name="U"))
        session.add(
            ChannelAccount(
                id=ACCOUNT, user_id=USER, channel="gmail", account_email="u@gmail.com",
                refresh_token_enc="enc", status="connected",
            )
        )
        session.add(UserSettings(user_id=USER, confidence_floor=0.75))
        for order, category in enumerate(DEFAULT_TAXONOMY):
            session.add(
                Category(
                    id=f"cat-{category['key']}", user_id=USER, key=category["key"],
                    name=category["name"], description=category["description"],
                    channel_label_name=f"ZeroInbox/{category['name']}",
                    default_action=category["default_action"], is_default=True,
                    sort_order=order,
                )
            )
        session.add(
            Rule(
                id="rule-substack", user_id=USER, name="Substack newsletters",
                kind="deterministic", source="seed_pack",
                matcher={"list_id": "substack.com"},
                action={"set_category": "newsletters", "archive": True},
                status="active", confidence=0.95,
            )
        )


class TestAssembly:
    def test_every_phase_1_node_is_wired(self):
        nodes = build_triage_graph().get_graph().nodes
        for name in (
            "load_context", "fetch_items", "redact_items", "apply_deterministic_rules",
            "apply_sender_history", "prepare_llm_batches", "llm_classify_batch",
            "deep_read_escalation", "cluster_decisions", "persist_decisions",
            "handle_error", "finalize",
        ):
            assert name in nodes

    def test_phase_2_review_nodes_are_wired(self):
        """Phase 2: the never-miss safeguards (second-pass reviewer + confidence
        floor) are live in the graph — this replaces the old Phase-1 scaffolding
        assertion that these nodes were absent."""
        nodes = triage_graph.get_graph().nodes
        assert "second_pass_reviewer" in nodes
        assert "apply_never_miss_floor" in nodes


class TestZeroTokenPath:
    def test_rules_resolve_everything_without_one_llm_call(self, seeded, monkeypatch):
        def explode():
            raise AssertionError("tier 3 must not run when tiers 1-2 resolved everything")

        monkeypatch.setattr("llm.client.get_llm_client", explode)

        items = [item(n) for n in range(12)]
        state = execute_triage(
            user_id=USER, channel_account_id=ACCOUNT, items=items, dry_run=True
        )

        assert state["status"] == "completed"
        assert state["counts"]["total"] == 12
        assert state["counts"]["by_tier"] == {"rule": 12}
        assert state["cost"]["llm_calls"] == 0

    def test_decisions_and_clusters_are_persisted_with_the_tier_that_fired(self, seeded):
        from db.models import Cluster, Decision, Item, TriageRun
        from db.session import create_db_session

        run_id = run_triage(
            user_id=USER, channel_account_id=ACCOUNT, items=[item(n) for n in range(12)]
        )

        with create_db_session() as session:
            decisions = list(session.execute(select(Decision)).scalars())
            clusters = list(session.execute(select(Cluster)).scalars())
            items_rows = list(session.execute(select(Item)).scalars())
            run = session.get(TriageRun, run_id)

            assert len(decisions) == 12
            assert len(items_rows) == 12
            assert all(d.decided_by == "rule" for d in decisions)
            assert all(d.rule_id == "rule-substack" for d in decisions)
            assert all(d.reasoning for d in decisions)
            assert all(d.cluster_id is not None for d in decisions)
            assert all(d.category_id == "cat-newsletters" for d in decisions)
            assert sum(c.item_count for c in clusters) == 12
            assert run.status == "completed"
            assert run.items_decided == 12
            assert run.dry_run is True

    def test_a_resumed_run_never_decides_a_thread_twice(self, seeded):
        from db.models import Decision
        from db.session import create_db_session

        items = [item(n) for n in range(12)]
        run_id = run_triage(user_id=USER, channel_account_id=ACCOUNT, items=items)
        run_triage(user_id=USER, channel_account_id=ACCOUNT, items=items, run_id=run_id)

        with create_db_session() as session:
            decisions = list(session.execute(select(Decision)).scalars())
            assert len(decisions) == 12
            assert len({d.item_id for d in decisions}) == 12


class TestNoRunWideReviewUpgrade:
    """Phase 9, item zero — the vacuous review gate.

    ``persist_decisions`` used to end every run with a run-wide "safety sweep":
    ``upgrade_review_state(run_id=..., state="reviewed")``. ``reviewed`` is a claim
    about a DECISION ("the never-miss reviewer audited this thread"), so the sweep
    vouched for every row the reviewer never saw — which is how 171 live ``digest``
    decisions read ``reviewed`` having never been looked at, 44 of them applied. The
    ``NotReviewedError`` gate then passed vacuously.

    These two tests cut that seam from both sides: the run must reach the end
    successfully (the sweep no longer raises ``ReviewScopeError`` into
    ``persist_decisions``'s outer ``try``, which is what made every run finish
    ``resumable``), and an unaudited row must come out the other side still
    ``provisional``.
    """

    RUN = "run-sweep"

    def _checkpoint(self, *, item_id, action, review_state):
        """A row already durable from its tier's checkpoint, as on a real run."""
        from db.models import Decision, Item
        from db.session import create_db_session

        with create_db_session() as session:
            session.add(
                Item(
                    id=item_id,
                    user_id=USER,
                    channel_account_id=ACCOUNT,
                    external_thread_id=f"thread-{item_id}",
                    subject="Weekly digest",
                    from_email="news@substack.com",
                    from_domain="substack.com",
                    snippet_redacted="Top stories",
                    internal_date=datetime.now(timezone.utc),
                )
            )
            session.flush()
            session.add(
                Decision(
                    id=f"dec-{item_id}",
                    user_id=USER,
                    run_id=self.RUN,
                    item_id=item_id,
                    category_id="cat-newsletters",
                    proposed_action=action,
                    confidence=0.9,
                    reasoning="seeded",
                    decided_by="rule",
                    time_sensitive=False,
                    status="proposed",
                    review_state=review_state,
                )
            )

    def _persist(self):
        from graph.nodes import persist_decisions

        return persist_decisions(
            {
                "run_id": self.RUN,
                "user_id": USER,
                "channel_account_id": ACCOUNT,
                "items": [],
                "clusters": [],
                "llm_calls": [],
                "decisions": [
                    {
                        "item_id": "it-audited",
                        "proposed_action": "archive",
                        "confidence": 0.9,
                        "reasoning": "seeded",
                        "decided_by": "rule",
                        "status": "proposed",
                        "category": "newsletters",
                    },
                    {
                        "item_id": "it-unaudited",
                        "proposed_action": "digest",
                        "confidence": 0.9,
                        "reasoning": "seeded",
                        "decided_by": "rule",
                        "status": "proposed",
                        "category": "newsletters",
                    },
                ],
                "review_failed_item_ids": [],
                "settings": {"confidence_floor": 0.75},
            }
        )

    @pytest.fixture
    def two_rows(self, seeded):
        from db.models import TriageRun
        from db.session import create_db_session

        with create_db_session() as session:
            session.add(
                TriageRun(
                    id=self.RUN,
                    user_id=USER,
                    channel_account_id=ACCOUNT,
                    status="running",
                    dry_run=True,
                    counts={},
                )
            )
        # One row the reviewer really audited, one it never reached.
        self._checkpoint(item_id="it-audited", action="archive", review_state="reviewed")
        self._checkpoint(
            item_id="it-unaudited", action="digest", review_state="provisional"
        )

    def test_finalisation_succeeds_without_a_run_wide_upgrade(self, two_rows):
        """Seam cut #1: reinstating the sweep makes this red.

        ``upgrade_review_state`` now REFUSES an un-narrowed promotion to
        ``reviewed`` (``ReviewScopeError``), and ``persist_decisions`` swallows
        every exception into ``{"error": ...}`` — so a reinstated sweep turns this
        assertion red and every real run ``resumable`` instead of ``completed``.
        """
        out = self._persist()

        assert out.get("error") is None, (
            "persist_decisions raised — a run-wide review upgrade is no longer "
            f"expressible: {out.get('error')!r}"
        )
        assert out["counts"]["total"] == 2

    def test_a_row_the_reviewer_never_audited_is_left_provisional(self, two_rows):
        """Seam cut #2: the sweep would flip ``it-unaudited`` to ``reviewed``."""
        from db.models import Decision
        from db.session import create_db_session

        self._persist()

        with create_db_session() as session:
            states = {
                row.item_id: row.review_state
                for row in session.execute(select(Decision)).scalars()
            }
        assert states["it-unaudited"] == "provisional", (
            "finalisation vouched for a thread the never-miss reviewer never saw"
        )
        assert states["it-audited"] == "reviewed", (
            "the reviewer's own verdict must survive finalisation untouched"
        )


class TestFailurePath:
    def test_a_fetch_failure_keeps_the_run_recoverable_and_hides_nothing(self, seeded):
        from db.models import TriageRun
        from db.session import create_db_session

        # No items supplied and no channel adapter connected -> fetch_items errors.
        state = execute_triage(user_id=USER, channel_account_id=ACCOUNT)

        assert state["status"] == "failed"
        assert state["counts"]["total"] == 0
        with create_db_session() as session:
            run = session.execute(select(TriageRun)).scalars().one()
            assert run.status == "failed"
            assert run.error_message


class TestSenderProfilePersistence:
    """Regression for HIGH #4: sender_history() must be wired into ingestion so
    sender_profiles rows are populated and the never-miss signal fires in real
    mailbox runs (not just fixture seeds)."""

    def test_persist_sender_profiles_upserts_rows(self, seeded):
        from db.models import SenderProfile
        from db.session import create_db_session

        signals = {
            "maya@northwind.io": {
                "received_count": 12,
                "opened_count": 10,
                "replied_count": 5,
                "ever_replied": True,
                "last_replied_at": datetime.now(timezone.utc),
                "archived_by_user_count": 0,
            },
            "promo@dealsdaily.com": {
                "received_count": 64,
                "opened_count": 0,
                "replied_count": 0,
                "ever_replied": False,
                "last_replied_at": None,
                "archived_by_user_count": 51,
            },
        }

        with create_db_session() as session:
            upserted = persist_sender_profiles(
                session, user_id=USER, signals=signals
            )
            assert len(upserted) == 2
            rows = list(session.execute(select(SenderProfile)).scalars().all())
            by_email = {r.sender_email: r for r in rows}
            assert by_email["maya@northwind.io"].ever_replied is True
            assert by_email["maya@northwind.io"].replied_count == 5
            assert by_email["maya@northwind.io"].importance_score > 0
            assert by_email["promo@dealsdaily.com"].ever_replied is False
            # Non-replied senders get a low score (not the never-miss band).
            assert by_email["promo@dealsdaily.com"].importance_score < 0.2

    def test_persist_sender_profiles_is_idempotent(self, seeded):
        from db.models import SenderProfile
        from db.session import create_db_session

        signal = {
            "dev@arcstack.dev": {
                "received_count": 3,
                "opened_count": 2,
                "replied_count": 1,
                "ever_replied": True,
                "last_replied_at": datetime.now(timezone.utc),
                "archived_by_user_count": 0,
            }
        }

        with create_db_session() as session:
            persist_sender_profiles(session, user_id=USER, signals=signal)
            persist_sender_profiles(session, user_id=USER, signals=signal)
            rows = list(session.execute(select(SenderProfile)).scalars().all())
            assert len(rows) == 1

