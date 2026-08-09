"""Unit tests for the category taxonomy: CRUD, default seeding, label-name
derivation, and 1:1 Gmail label sync — spec/capabilities/taxonomy-management.md.

The Gmail label client is faked; no real API call is made in these unit tests
(sync-labels against a real mailbox is covered by the integration suite owned by
the gmail-mutations slice).
"""

from __future__ import annotations

import pytest

from channels.gmail.labels import label_name_for
from tools.rules import DEFAULT_CATEGORY_KEYS
from tools.taxonomy import (
    TaxonomyError,
    create_category,
    delete_category,
    ensure_default_taxonomy,
    list_categories,
    sync_labels,
    update_category,
)

USER_ID = "user-taxonomy"
OTHER_USER_ID = "user-other"


def _session():
    import db.session as session_module

    return session_module._SessionLocal()


class FakeLabelClient:
    """A minimal fake matching GmailLabelManager's surface."""

    def __init__(self):
        self._labels: dict[str, dict] = {}
        self._next_id = 1

    def ensure_label(self, name: str) -> dict:
        for label in self._labels.values():
            if label["name"] == name:
                return label
        label_id = f"Label_{self._next_id}"
        self._next_id += 1
        label = {"id": label_id, "name": name}
        self._labels[label_id] = label
        return label

    def rename_label(self, label_id: str, new_name: str) -> dict:
        if label_id not in self._labels:
            raise LookupError(f"no such label {label_id}")
        self._labels[label_id]["name"] = new_name
        return self._labels[label_id]

    def list_zero_inbox_labels(self) -> list[dict]:
        return list(self._labels.values())


class FailingLabelClient(FakeLabelClient):
    def ensure_label(self, name: str) -> dict:
        raise RuntimeError("Gmail is down")


# --- label-name derivation ---------------------------------------------------


class TestLabelNameDerivation:
    def test_label_name_is_namespaced(self):
        assert label_name_for("Newsletters") == "ZeroInbox/Newsletters"

    def test_label_name_preserves_spaces(self):
        assert label_name_for("My Category") == "ZeroInbox/My Category"


# --- default seeding -----------------------------------------------------------


class TestDefaultSeeding:
    def test_new_user_is_seeded_with_six_defaults(self, _isolated_db):
        with _session() as session:
            created = ensure_default_taxonomy(session, USER_ID)
            session.commit()
            rows = list_categories(session, USER_ID)

        assert created == 6
        assert {r.key for r in rows} == set(DEFAULT_CATEGORY_KEYS)
        for row in rows:
            assert row.channel_label_name == f"ZeroInbox/{row.name}"

    def test_seeding_is_idempotent(self, _isolated_db):
        with _session() as session:
            ensure_default_taxonomy(session, USER_ID)
            session.commit()
            created_again = ensure_default_taxonomy(session, USER_ID)
            session.commit()

        assert created_again == 0

    def test_urgent_default_action_is_never_archive(self, _isolated_db):
        with _session() as session:
            ensure_default_taxonomy(session, USER_ID)
            session.commit()
            rows = list_categories(session, USER_ID)

        urgent = next(r for r in rows if r.key == "urgent")
        assert urgent.default_action != "archive"


# --- CRUD ------------------------------------------------------------------


class TestCategoryCrud:
    def test_create_category_happy_path(self, _isolated_db):
        with _session() as session:
            category = create_category(
                session,
                USER_ID,
                key="finance",
                name="Finance",
                description="Bills and statements",
                default_action="keep",
            )
            channel_label_name = category.channel_label_name
            is_default = category.is_default
            session.commit()

        assert channel_label_name == "ZeroInbox/Finance"
        assert is_default is False

    def test_create_category_duplicate_key_is_rejected(self, _isolated_db):
        with _session() as session:
            create_category(session, USER_ID, key="finance", name="Finance")
            session.commit()

        with _session() as session:
            with pytest.raises(TaxonomyError):
                create_category(session, USER_ID, key="finance", name="Finance Again")

    def test_create_category_missing_name_is_rejected(self, _isolated_db):
        with _session() as session:
            with pytest.raises(TaxonomyError):
                create_category(session, USER_ID, key="k", name="")

    def test_create_urgent_with_archive_action_is_rejected(self, _isolated_db):
        with _session() as session:
            with pytest.raises(TaxonomyError):
                create_category(
                    session,
                    USER_ID,
                    key="urgent",
                    name="Urgent",
                    default_action="archive",
                )

    def test_update_category_renames_and_updates_label_name(self, _isolated_db):
        with _session() as session:
            category = create_category(session, USER_ID, key="finance", name="Finance")
            session.commit()
            category_id = category.id

        with _session() as session:
            updated = update_category(session, USER_ID, category_id, name="Money")
            updated_name = updated.name
            updated_label_name = updated.channel_label_name
            session.commit()

        assert updated_name == "Money"
        assert updated_label_name == "ZeroInbox/Money"

    def test_update_urgent_to_archive_is_rejected(self, _isolated_db):
        with _session() as session:
            ensure_default_taxonomy(session, USER_ID)
            session.commit()
            urgent = next(r for r in list_categories(session, USER_ID) if r.key == "urgent")
            urgent_id = urgent.id

        with _session() as session:
            with pytest.raises(TaxonomyError):
                update_category(session, USER_ID, urgent_id, default_action="archive")

    def test_update_category_scoped_to_owning_user(self, _isolated_db):
        with _session() as session:
            category = create_category(session, USER_ID, key="finance", name="Finance")
            session.commit()
            category_id = category.id

        with _session() as session:
            with pytest.raises(LookupError):
                update_category(session, OTHER_USER_ID, category_id, name="Stolen")

    def test_delete_category_removes_row_only(self, _isolated_db):
        with _session() as session:
            category = create_category(session, USER_ID, key="finance", name="Finance")
            session.commit()
            category_id = category.id

        with _session() as session:
            delete_category(session, USER_ID, category_id)
            session.commit()
            remaining = list_categories(session, USER_ID)

        assert all(r.id != category_id for r in remaining)

    def test_delete_category_scoped_to_owning_user(self, _isolated_db):
        with _session() as session:
            category = create_category(session, USER_ID, key="finance", name="Finance")
            session.commit()
            category_id = category.id

        with _session() as session:
            with pytest.raises(LookupError):
                delete_category(session, OTHER_USER_ID, category_id)

    def test_categories_are_isolated_per_user(self, _isolated_db):
        with _session() as session:
            create_category(session, USER_ID, key="finance", name="Finance")
            session.commit()

        with _session() as session:
            other_rows = list_categories(session, OTHER_USER_ID)

        assert other_rows == []


# --- label sync ----------------------------------------------------------------


class TestSyncLabels:
    def test_sync_creates_a_label_for_every_category(self, _isolated_db):
        with _session() as session:
            ensure_default_taxonomy(session, USER_ID)
            session.commit()

            client = FakeLabelClient()
            results = sync_labels(session, USER_ID, client)
            session.commit()

            rows = list_categories(session, USER_ID)

        assert len(results) == 6
        assert all(r["status"] == "synced" for r in results)
        assert all(row.channel_label_id is not None for row in rows)
        assert {label["name"] for label in client.list_zero_inbox_labels()} == {
            f"ZeroInbox/{row.name}" for row in rows
        }

    def test_sync_is_idempotent_and_does_not_duplicate_labels(self, _isolated_db):
        with _session() as session:
            ensure_default_taxonomy(session, USER_ID)
            session.commit()

            client = FakeLabelClient()
            sync_labels(session, USER_ID, client)
            session.commit()
            sync_labels(session, USER_ID, client)
            session.commit()

        assert len(client.list_zero_inbox_labels()) == 6

    def test_sync_renames_label_after_category_rename(self, _isolated_db):
        with _session() as session:
            category = create_category(session, USER_ID, key="finance", name="Finance")
            session.commit()
            category_id = category.id

            client = FakeLabelClient()
            sync_labels(session, USER_ID, client)
            session.commit()

        with _session() as session:
            update_category(session, USER_ID, category_id, name="Money")
            session.commit()

            sync_labels(session, USER_ID, client)
            session.commit()

        assert client.list_zero_inbox_labels()[0]["name"] == "ZeroInbox/Money"

    def test_sync_failure_on_one_category_does_not_block_others(self, _isolated_db):
        with _session() as session:
            ensure_default_taxonomy(session, USER_ID)
            session.commit()

            client = FailingLabelClient()
            results = sync_labels(session, USER_ID, client)
            session.commit()

        assert len(results) == 6
        assert all(r["status"] == "unsynced" for r in results)
        assert all("error" in r for r in results)
