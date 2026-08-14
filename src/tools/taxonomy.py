"""Category taxonomy: CRUD against the DB + 1:1 sync to real Gmail labels.

spec/capabilities/taxonomy-management.md is the source of truth:

- Default taxonomy ships six editable/deletable categories (``tools.rules.DEFAULT_TAXONOMY``).
- Every category maps to exactly one Gmail label, namespaced ``ZeroInbox/<Name>``.
  Creating a category creates the label; renaming renames it; **deleting a category
  never deletes the label or any mail** — the label is left in place.
- Gmail's label sidebar is the master list; there is no separate archive table.
- ``Urgent`` can never carry ``default_action = archive``.

This module is transport-agnostic: label sync is delegated to any object exposing
the ``GmailLabelManager`` surface (``ensure_label``, ``rename_label``,
``list_zero_inbox_labels``) so it is trivially unit-testable with a fake client and
reusable from ``src/api/categories.py``.
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from channels.gmail.labels import label_name_for
from tools.rules import DEFAULT_TAXONOMY, VALID_ACTIONS

URGENT_KEY = "urgent"

#: Categories that may NEVER carry ``default_action="archive"``.
#:
#: Urgent was guarded from the start; People and Legal were not — and that gap
#: was found live, with a real account holding ``people -> archive``. Anything
#: that writes a category (the taxonomy editor, the LLM propose endpoint, a
#: future import) could silently flip the one category that means "a human
#: wrote to you", after which the agent would auto-archive first-contact mail
#: from real people. The never-miss layer would still hold anyone previously
#: replied to, any VIP and anything time-sensitive — but a stranger's genuine
#: first email is exactly the mail this product must never lose.
NEVER_ARCHIVE_KEYS: frozenset[str] = frozenset({URGENT_KEY, "people", "legal"})


class _Unset:
    """Sentinel: "this field was not supplied" — distinct from an explicit None."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "UNSET"


UNSET = _Unset()


class TaxonomyError(ValueError):
    """A taxonomy business-rule violation (e.g. Urgent set to archive)."""


class LabelClient(Protocol):
    def ensure_label(self, name: str) -> dict: ...
    def rename_label(self, label_id: str, new_name: str) -> dict: ...
    def list_zero_inbox_labels(self) -> list[dict]: ...


def _models():
    from db import models

    return models


# --- seeding -------------------------------------------------------------------


def ensure_default_taxonomy(session: Session, user_id: str) -> int:
    """Insert any missing default categories for ``user_id``. Returns rows created.

    Delegates to db.seed so there is exactly one seeding implementation shared by
    OAuth-connect time and the taxonomy API.
    """
    from db.seed import ensure_default_taxonomy as _seed

    return _seed(session, user_id)


# --- CRUD ------------------------------------------------------------------


def list_categories(session: Session, user_id: str) -> list:
    Category = _models().Category
    return (
        session.execute(
            select(Category)
            .where(Category.user_id == user_id)
            .order_by(Category.sort_order, Category.name)
        )
        .scalars()
        .all()
    )


def _validate_action(key: str, default_action: str) -> None:
    if default_action not in VALID_ACTIONS:
        raise TaxonomyError(f"default_action must be one of {list(VALID_ACTIONS)}")
    if key in NEVER_ARCHIVE_KEYS and default_action == "archive":
        raise TaxonomyError(
            f"The {key.title()} category can never carry default_action=archive — "
            "it is mail a human must see."
        )


def validate_auto_act_threshold(value: float | None) -> float | None:
    """Phase 7 Rule A5: ``0 < auto_act_threshold <= 1``. ``None`` = inherit global.

    Returns the normalised float so callers can assign the result directly.
    """
    if value is None:
        return None
    try:
        threshold = float(value)
    except (TypeError, ValueError) as exc:
        raise TaxonomyError("auto_act_threshold must be a number between 0 and 1") from exc
    if not (0 < threshold <= 1):
        raise TaxonomyError("auto_act_threshold must be greater than 0 and at most 1")
    return threshold


def create_category(
    session: Session,
    user_id: str,
    *,
    key: str,
    name: str,
    description: str = "",
    default_action: str = "keep",
    sort_order: int = 0,
    auto_act_threshold: float | None = None,
) -> object:
    Category = _models().Category

    if not key or not key.strip():
        raise TaxonomyError("key is required")
    if not name or not name.strip():
        raise TaxonomyError("name is required")
    _validate_action(key, default_action)
    auto_act_threshold = validate_auto_act_threshold(auto_act_threshold)

    existing = session.execute(
        select(Category).where(Category.user_id == user_id, Category.key == key)
    ).scalar_one_or_none()
    if existing is not None:
        raise TaxonomyError(f"category key {key!r} already exists")

    category = Category(
        user_id=user_id,
        key=key,
        name=name,
        description=description,
        channel_label_name=label_name_for(name),
        default_action=default_action,
        auto_act_threshold=auto_act_threshold,
        is_default=False,
        sort_order=sort_order,
    )
    session.add(category)
    session.flush()
    return category


def update_category(
    session: Session,
    user_id: str,
    category_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    default_action: str | None = None,
    sort_order: int | None = None,
    auto_act_threshold: float | None | object = UNSET,
) -> object:
    Category = _models().Category
    category = session.get(Category, category_id)
    if category is None or category.user_id != user_id:
        raise LookupError("category not found")

    next_action = default_action if default_action is not None else category.default_action
    _validate_action(category.key, next_action)
    if auto_act_threshold is not UNSET:
        # ``None`` here is meaningful — it clears the override so the category
        # inherits the global bar again — hence the UNSET sentinel.
        category.auto_act_threshold = validate_auto_act_threshold(auto_act_threshold)

    if name is not None and name.strip():
        category.name = name
        category.channel_label_name = label_name_for(name)
    if description is not None:
        category.description = description
    if default_action is not None:
        category.default_action = default_action
    if sort_order is not None:
        category.sort_order = sort_order
    session.flush()
    return category


def delete_category(session: Session, user_id: str, category_id: str) -> None:
    """Deletes the category row only — the Gmail label and all mail are left intact."""
    Category = _models().Category
    category = session.get(Category, category_id)
    if category is None or category.user_id != user_id:
        raise LookupError("category not found")
    session.delete(category)
    session.flush()


# --- label sync --------------------------------------------------------------


def sync_labels(session: Session, user_id: str, label_client: LabelClient) -> list[dict]:
    """Ensures every category has a real ``ZeroInbox/<Name>`` Gmail label.

    Creates the label when missing (first sync / newly created category), renames
    it when the category's ``channel_label_name`` no longer matches the stored
    ``channel_label_id``'s label (i.e. the category was renamed since the last
    sync). Never deletes a label. On a per-category failure the category keeps
    its prior state (unsynced) and the loop continues — one bad category must
    never block the rest of the taxonomy from syncing.
    """
    results: list[dict] = []
    for category in list_categories(session, user_id):
        wanted_name = category.channel_label_name or label_name_for(category.name)
        try:
            if category.channel_label_id:
                label = label_client.rename_label(category.channel_label_id, wanted_name)
            else:
                label = label_client.ensure_label(wanted_name)
            category.channel_label_id = label["id"]
            category.channel_label_name = label["name"]
            results.append({"category_id": category.id, "status": "synced", "label_id": label["id"]})
        except Exception as exc:  # noqa: BLE001 - keep the category usable, report and continue
            results.append(
                {"category_id": category.id, "status": "unsynced", "error": str(exc)}
            )
    session.flush()
    return results


__all__ = [
    "DEFAULT_TAXONOMY",
    "TaxonomyError",
    "LabelClient",
    "URGENT_KEY",
    "UNSET",
    "validate_auto_act_threshold",
    "ensure_default_taxonomy",
    "list_categories",
    "create_category",
    "update_category",
    "delete_category",
    "sync_labels",
]
