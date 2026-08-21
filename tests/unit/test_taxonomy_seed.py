"""Default-taxonomy seeding (spec/capabilities/taxonomy-management.md).

"Seed defaults on first sign-in: Finance, Newsletters, Notifications, Personal,
Shopping, Travel, Needs review." Rules are ``label_only`` | ``label_and_archive``
(Newsletters auto-archive by default, Finance label-only). "Needs review" is
reserved (``is_needs_review``), rule fixed ``label_only``.
"""

from __future__ import annotations

from sqlalchemy import select

from db.models import Category
from db.seed import (
    DEFAULT_CATEGORY_NAMES,
    DEFAULT_TAXONOMY,
    NEEDS_REVIEW_NAME,
    VALID_RULES,
    ensure_default_taxonomy,
)

USER_ID = "test-user-seed"

EXPECTED_NAMES = (
    "Finance",
    "Newsletters",
    "Notifications",
    "Personal",
    "Shopping",
    "Travel",
    "Needs review",
)


def _categories(session, user_id=USER_ID):
    return list(
        session.execute(
            select(Category).where(Category.user_id == user_id).order_by(Category.position)
        ).scalars()
    )


class TestSeedSet:
    def test_a_new_user_gets_the_spec_seed_set_in_order(self, db_session):
        created = ensure_default_taxonomy(db_session, USER_ID)
        db_session.commit()
        rows = _categories(db_session)

        assert created == len(DEFAULT_TAXONOMY) == 7
        assert tuple(r.name for r in rows) == EXPECTED_NAMES == DEFAULT_CATEGORY_NAMES
        assert all(r.rule in VALID_RULES for r in rows)
        assert all(r.description for r in rows)  # classifier guidance is never empty

    def test_default_rules_newsletters_archive_finance_label_only(self, db_session):
        ensure_default_taxonomy(db_session, USER_ID)
        db_session.commit()
        by_name = {r.name: r for r in _categories(db_session)}

        assert by_name["Newsletters"].rule == "label_and_archive"
        assert by_name["Finance"].rule == "label_only"
        assert by_name[NEEDS_REVIEW_NAME].rule == "label_only"

    def test_exactly_one_reserved_needs_review_category(self, db_session):
        ensure_default_taxonomy(db_session, USER_ID)
        db_session.commit()
        rows = _categories(db_session)
        reserved = [r for r in rows if r.is_needs_review]
        assert len(reserved) == 1
        assert reserved[0].name == NEEDS_REVIEW_NAME


class TestIdempotencyAndIsolation:
    def test_seeding_twice_creates_nothing_new(self, db_session):
        ensure_default_taxonomy(db_session, USER_ID)
        assert ensure_default_taxonomy(db_session, USER_ID) == 0
        db_session.commit()
        assert len(_categories(db_session)) == len(DEFAULT_TAXONOMY)

    def test_user_edits_are_never_touched(self, db_session):
        ensure_default_taxonomy(db_session, USER_ID)
        db_session.commit()
        rows = _categories(db_session)
        # User renames Finance and deletes Travel — reseeding must not undo either.
        finance = next(r for r in rows if r.name == "Finance")
        finance.name = "Money"
        db_session.delete(next(r for r in rows if r.name == "Travel"))
        db_session.commit()

        assert ensure_default_taxonomy(db_session, USER_ID) == 0
        db_session.commit()
        names = {r.name for r in _categories(db_session)}
        assert "Money" in names and "Finance" not in names and "Travel" not in names

    def test_missing_reserved_category_is_restored(self, db_session):
        """The classifier and API rely on "Needs review" existing; if it is ever
        absent (e.g. rows created by hand), seeding restores it — and only it."""
        db_session.add(
            Category(user_id=USER_ID, name="Work", rule="label_only", position=0)
        )
        db_session.commit()

        assert ensure_default_taxonomy(db_session, USER_ID) == 1
        db_session.commit()
        rows = _categories(db_session)
        assert {r.name for r in rows} == {"Work", NEEDS_REVIEW_NAME}
        restored = next(r for r in rows if r.is_needs_review)
        assert restored.rule == "label_only"

    def test_two_users_get_isolated_seed_rows(self, db_session):
        ensure_default_taxonomy(db_session, "test-user-a")
        ensure_default_taxonomy(db_session, "test-user-b")
        db_session.commit()
        a = _categories(db_session, "test-user-a")
        b = _categories(db_session, "test-user-b")

        assert len(a) == len(b) == len(DEFAULT_TAXONOMY)
        assert {r.id for r in a}.isdisjoint({r.id for r in b})
