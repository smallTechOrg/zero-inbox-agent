"""Default-taxonomy seeding — regression for the empty `categories` table.

The Phase-1 gate found 0 category rows, so every decisions.category_id was NULL
and the UI showed category:null. spec/capabilities/taxonomy-management.md:
"A new user is seeded with the default categories."
"""

from __future__ import annotations

from sqlalchemy import select

from db.seed import ensure_default_taxonomy
from tools.rules import DEFAULT_CATEGORY_KEYS, DEFAULT_TAXONOMY

#: Bound to the canonical list rather than hardcoded, so adding a seed category
#: (Phase 9 added ``important``) cannot make this assertion drift out of date
#: while still asserting an EXACT count.
SEED_COUNT = len(DEFAULT_TAXONOMY)

USER_ID = "user-seed"


def _session():
    import db.session as session_module

    return session_module._SessionLocal()


def _categories(session, user_id=USER_ID):
    from db.models import Category

    return list(
        session.execute(select(Category).where(Category.user_id == user_id)).scalars()
    )


class TestEnsureDefaultTaxonomy:
    def test_a_new_user_gets_exactly_the_defaults(self, _isolated_db):
        with _session() as session:
            created = ensure_default_taxonomy(session, USER_ID)
            session.commit()
            rows = _categories(session)

        assert created == SEED_COUNT
        assert {r.key for r in rows} == set(DEFAULT_CATEGORY_KEYS)
        assert all(r.is_default for r in rows)
        assert all(r.channel_label_name == f"ZeroInbox/{r.name}" for r in rows)
        # The Urgent category can never carry default_action=archive.
        urgent = next(r for r in rows if r.key == "urgent")
        assert urgent.default_action == "keep"

    def test_seeding_is_idempotent_across_calls(self, _isolated_db):
        with _session() as session:
            ensure_default_taxonomy(session, USER_ID)
            assert ensure_default_taxonomy(session, USER_ID) == 0
            session.commit()
            assert len(_categories(session)) == SEED_COUNT

    def test_backfill_fills_only_the_missing_keys_and_never_touches_user_edits(
        self, _isolated_db
    ):
        from db.models import Category

        with _session() as session:
            # An existing user with one edited default and one custom category.
            session.add(
                Category(
                    id="cat-custom",
                    user_id=USER_ID,
                    key="newsletters",
                    name="My Newsletters",
                    description="user-edited",
                    default_action="keep",
                    is_default=False,
                    sort_order=99,
                )
            )
            session.add(
                Category(
                    id="cat-invoices",
                    user_id=USER_ID,
                    key="invoices",
                    name="Invoices",
                    description="user-created",
                    default_action="keep",
                )
            )
            session.flush()

            created = ensure_default_taxonomy(session, USER_ID)
            session.commit()
            rows = _categories(session)

        assert created == SEED_COUNT - 1  # newsletters already present
        # seeded + the edited newsletters row + the custom invoices row
        assert len(rows) == SEED_COUNT + 1
        edited = next(r for r in rows if r.key == "newsletters")
        assert edited.name == "My Newsletters" and edited.is_default is False

    def test_two_users_taxonomies_never_interact(self, _isolated_db):
        with _session() as session:
            ensure_default_taxonomy(session, "user-a")
            ensure_default_taxonomy(session, "user-b")
            session.commit()
            assert len(_categories(session, "user-a")) == SEED_COUNT
            assert len(_categories(session, "user-b")) == SEED_COUNT


class TestSeedingEntryPoints:
    def test_oauth_upsert_seeds_the_taxonomy_for_a_new_user(self, _isolated_db):
        from channels.gmail.store import SqlConnectionStore
        from security.crypto import TokenCipher

        store = SqlConnectionStore(cipher=TokenCipher("unit-test-secret"))
        user_id, _connection_id = store.upsert_user_and_connection(
            email="new@example.com",
            display_name="New",
            refresh_token_enc="enc",
            scopes=["gmail.readonly"],
        )
        with _session() as session:
            assert {r.key for r in _categories(session, user_id)} == set(
                DEFAULT_CATEGORY_KEYS
            )

    def test_reconnecting_does_not_duplicate_categories(self, _isolated_db):
        from channels.gmail.store import SqlConnectionStore
        from security.crypto import TokenCipher

        store = SqlConnectionStore(cipher=TokenCipher("unit-test-secret"))
        for _ in range(2):
            user_id, _ = store.upsert_user_and_connection(
                email="again@example.com",
                display_name="Again",
                refresh_token_enc="enc",
                scopes=["gmail.readonly"],
            )
        with _session() as session:
            assert len(_categories(session, user_id)) == SEED_COUNT

    def test_load_context_backfills_an_unseeded_existing_user(self, _isolated_db):
        """The user in production connected before seeding existed — their next
        run must materialise the taxonomy without duplicates."""
        from db.session import create_db_session
        from graph.persistence import load_context

        with create_db_session() as session:
            context = load_context(session, USER_ID)

        assert {c["key"] for c in context["categories"]} == set(DEFAULT_CATEGORY_KEYS)
        # Real rows with real ids — not the in-memory fallback list.
        assert all(c["id"] is not None for c in context["categories"])
        with _session() as session:
            assert len(_categories(session)) == SEED_COUNT

    def test_persist_run_results_resolves_category_ids_via_the_safety_net(
        self, _isolated_db
    ):
        """Regression for BOTH the NULL category_id defect and the dead
        `category_key` write: decisions map to seeded category rows by key, and
        no phantom column is involved."""
        from sqlalchemy import inspect as sa_inspect, select

        from db.models import Decision
        from db.session import create_db_session
        from graph.persistence import persist_run_results

        with create_db_session() as session:
            persist_run_results(
                session,
                run_id="run-x",
                user_id=USER_ID,
                channel_account_id="acct-x",
                items=[
                    {
                        "id": "item-1",
                        "external_thread_id": "thread-1",
                        "subject": "s",
                        "from_email": "a@b.c",
                        "snippet_redacted": "snip",
                    }
                ],
                decisions=[
                    {
                        "item_id": "item-1",
                        "category": "newsletters",
                        "proposed_action": "archive",
                        "confidence": 0.9,
                        "reasoning": "r",
                        "decided_by": "llm",
                        "rule_id": None,
                        "time_sensitive": False,
                        "status": "proposed",
                    }
                ],
                clusters=[],
                counts={},
                cost={},
                llm_calls=[],
                status="running",
            )

        with _session() as session:
            decision = session.execute(select(Decision)).scalars().one()
            category = _categories(session)
            assert decision.category_id is not None
            assert decision.category_id in {c.id for c in category if c.key == "newsletters"}
        # decisions has no category_key column — spec/data.md agrees.
        assert "category_key" not in {
            attr.key for attr in sa_inspect(Decision).mapper.column_attrs
        }
