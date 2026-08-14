"""Per-category autonomy threshold validation, and the Urgent invariant it must
not erode — spec/capabilities/drive-to-inbox-zero.md Rules A4 + A5.
"""

from __future__ import annotations

import pytest

from db.seed import SEEDED_AUTO_ACT_THRESHOLDS, ensure_default_taxonomy
from tools.taxonomy import (
    UNSET,
    TaxonomyError,
    create_category,
    list_categories,
    update_category,
    validate_auto_act_threshold,
)

USER_ID = "user-autonomy"


def _session():
    import db.session as session_module

    return session_module._SessionLocal()


# --- Rule A5: 0 < auto_act_threshold <= 1 ------------------------------------


@pytest.mark.parametrize("value", [0.01, 0.75, 0.8, 0.85, 0.999, 1.0])
def test_valid_thresholds_are_accepted(value):
    assert validate_auto_act_threshold(value) == pytest.approx(value)


@pytest.mark.parametrize("value", [0, 0.0, -0.1, -1, 1.01, 2])
def test_out_of_range_thresholds_are_rejected(value):
    with pytest.raises(TaxonomyError):
        validate_auto_act_threshold(value)


def test_a_non_numeric_threshold_is_rejected():
    with pytest.raises(TaxonomyError):
        validate_auto_act_threshold("high")


def test_none_means_inherit_the_global_bar_and_is_allowed():
    assert validate_auto_act_threshold(None) is None


# --- CRUD -------------------------------------------------------------------


def test_creating_a_category_persists_its_threshold():
    with _session() as session:
        category = create_category(
            session, USER_ID, key="legal", name="Legal", default_action="keep",
            auto_act_threshold=0.9,
        )
        session.commit()
        assert category.auto_act_threshold == pytest.approx(0.9)


def test_creating_a_category_without_a_threshold_inherits_the_global_bar():
    with _session() as session:
        category = create_category(session, USER_ID, key="misc", name="Misc")
        session.commit()
        assert category.auto_act_threshold is None


def test_creating_a_category_with_an_invalid_threshold_is_rejected():
    with _session() as session:
        with pytest.raises(TaxonomyError):
            create_category(
                session, USER_ID, key="bad", name="Bad", auto_act_threshold=0
            )


def test_updating_a_threshold_persists_it():
    with _session() as session:
        category = create_category(session, USER_ID, key="promos", name="Promos",
                                   default_action="archive")
        session.commit()
        update_category(session, USER_ID, category.id, auto_act_threshold=0.95)
        session.commit()
        assert category.auto_act_threshold == pytest.approx(0.95)


def test_updating_a_threshold_to_null_clears_the_override():
    with _session() as session:
        category = create_category(session, USER_ID, key="promos2", name="Promos2",
                                   default_action="archive", auto_act_threshold=0.9)
        session.commit()
        update_category(session, USER_ID, category.id, auto_act_threshold=None)
        session.commit()
        assert category.auto_act_threshold is None


def test_omitting_the_threshold_on_update_leaves_it_untouched():
    with _session() as session:
        category = create_category(session, USER_ID, key="promos3", name="Promos3",
                                   default_action="archive", auto_act_threshold=0.9)
        session.commit()
        update_category(session, USER_ID, category.id, name="Renamed",
                        auto_act_threshold=UNSET)
        session.commit()
        assert category.name == "Renamed"
        assert category.auto_act_threshold == pytest.approx(0.9)


def test_an_invalid_threshold_on_update_is_rejected():
    with _session() as session:
        category = create_category(session, USER_ID, key="promos4", name="Promos4")
        session.commit()
        with pytest.raises(TaxonomyError):
            update_category(session, USER_ID, category.id, auto_act_threshold=1.5)


# --- Rule A4: Urgent stays structurally un-archivable ------------------------


def test_urgent_can_never_be_created_as_archive():
    with _session() as session:
        with pytest.raises(TaxonomyError):
            create_category(session, USER_ID, key="urgent", name="Urgent",
                            default_action="archive")


def test_urgent_can_never_be_updated_to_archive_even_with_a_threshold_supplied():
    with _session() as session:
        ensure_default_taxonomy(session, USER_ID)
        session.commit()
        urgent = next(c for c in list_categories(session, USER_ID) if c.key == "urgent")
        with pytest.raises(TaxonomyError):
            update_category(session, USER_ID, urgent.id, default_action="archive",
                            auto_act_threshold=0.99)


def test_a_threshold_on_urgent_is_accepted_but_inert_because_it_stays_keep():
    with _session() as session:
        ensure_default_taxonomy(session, USER_ID)
        session.commit()
        urgent = next(c for c in list_categories(session, USER_ID) if c.key == "urgent")
        update_category(session, USER_ID, urgent.id, auto_act_threshold=0.5)
        session.commit()
        assert urgent.default_action == "keep"


# --- seeding ----------------------------------------------------------------


def test_the_default_taxonomy_seeds_the_documented_per_category_bars():
    with _session() as session:
        ensure_default_taxonomy(session, USER_ID)
        session.commit()
        by_key = {c.key: c for c in list_categories(session, USER_ID)}

    assert by_key["outreach"].auto_act_threshold == pytest.approx(0.85)
    assert by_key["receipts"].auto_act_threshold == pytest.approx(0.85)
    for key in ("newsletters", "notifications", "people", "urgent"):
        assert by_key[key].auto_act_threshold is None, key


def test_the_seed_table_matches_the_spec_exactly():
    assert SEEDED_AUTO_ACT_THRESHOLDS == {"outreach": 0.85, "receipts": 0.85}


# --- never-archive guard (found live: a real account had people -> archive) ---


import pytest as _pytest


@_pytest.mark.parametrize("key", ["urgent", "people", "legal"])
def test_a_human_facing_category_can_never_be_set_to_archive(_isolated_db, key):
    """Regression, found on a real account: `psykrsna@gmail.com` held
    `people -> archive` and `legal -> archive`. Only Urgent was guarded, so
    anything that writes a category — the taxonomy editor, the LLM propose
    endpoint, an import — could silently flip the one category meaning "a human
    wrote to you", after which first-contact mail from real people would be
    auto-archived. `categories` has no timestamps, so it left no audit trail."""
    from db.session import create_db_session
    from tools.taxonomy import TaxonomyError, create_category

    with create_db_session() as session:
        with _pytest.raises(TaxonomyError, match="can never carry default_action=archive"):
            create_category(
                session,
                "u-guard",
                key=key,
                name=key.title(),
                description="",
                default_action="archive",
            )


@_pytest.mark.parametrize("key", ["urgent", "people", "legal"])
def test_the_guard_also_blocks_an_update_not_just_a_create(_isolated_db, key):
    """The live flip happened to an EXISTING row, so create-time validation
    alone would not have prevented it."""
    from db.session import create_db_session
    from tools.taxonomy import TaxonomyError, create_category, update_category

    with create_db_session() as session:
        cat = create_category(
            session, "u-guard2", key=key, name=key.title(), description="",
            default_action="keep",
        )
        session.commit()
        cat_id = cat.id

    with create_db_session() as session:
        with _pytest.raises(TaxonomyError, match="can never carry default_action=archive"):
            update_category(session, "u-guard2", cat_id, default_action="archive")


def test_an_ordinary_category_may_still_archive(_isolated_db):
    """The guard must not become a blanket ban — archiving is the whole point."""
    from db.session import create_db_session
    from tools.taxonomy import create_category

    with create_db_session() as session:
        cat = create_category(
            session, "u-guard3", key="newsletters", name="Newsletters",
            description="", default_action="archive",
        )
        assert cat.default_action == "archive"
