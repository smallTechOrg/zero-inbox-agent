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

    def test_phase_2_review_nodes_are_absent_in_phase_1(self):
        nodes = triage_graph.get_graph().nodes
        assert "second_pass_reviewer" not in nodes
        assert "apply_never_miss_floor" not in nodes


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

