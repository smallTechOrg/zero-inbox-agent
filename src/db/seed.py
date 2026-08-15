"""Default-taxonomy seeding (spec/capabilities/taxonomy-management.md).

    Seed defaults on first sign-in: Finance, Newsletters, Notifications,
    Personal, Shopping, Travel, Needs review.

Each category carries a per-category ``rule`` — ``label_only`` or
``label_and_archive`` (Newsletters auto-archive by default; Finance label-only
by default). "Needs review" is reserved: never deletable, rule fixed
``label_only``.

Seeding is **first-sign-in** semantics: a user with ANY existing category is
left completely alone (the model keys categories by ``(user_id, name)``, so a
rename must never be "backfilled" away). The reserved "Needs review" row is the
one exception — identified structurally by ``is_needs_review``, it is restored
if missing because the classifier and the API both rely on its existence.

Called at OAuth-callback time (auth slice) and lazily by ``GET /api/taxonomy``.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

#: Gmail labels the agent creates are namespaced under this prefix so all agent
#: state in Gmail is identifiable and fully removable
#: (spec/capabilities/taxonomy-management.md, "Assumed": ``ZI/<Category>``).
LABEL_PREFIX = "ZI/"

#: The two per-category rules (spec/data.md § categories).
VALID_RULES: tuple[str, ...] = ("label_only", "label_and_archive")

NEEDS_REVIEW_NAME = "Needs review"

#: The seed set, in display order. Only Newsletters auto-archives by default;
#: everything else starts conservative (label only) — the user can flip any
#: rule except "Needs review"'s from the taxonomy panel.
DEFAULT_TAXONOMY: list[dict] = [
    {
        "name": "Finance",
        "rule": "label_only",
        "description": "Bank statements, bills, invoices, payment confirmations, "
        "tax and billing mail. Stays in the inbox: label only.",
    },
    {
        "name": "Newsletters",
        "rule": "label_and_archive",
        "description": "Subscribed bulk mail: newsletters, digests, marketing "
        "blasts — anything with an unsubscribe link you opted into.",
    },
    {
        "name": "Notifications",
        "rule": "label_only",
        "description": "Automated app and service notifications: social updates, "
        "CI results, delivery updates, calendar and account noise.",
    },
    {
        "name": "Personal",
        "rule": "label_only",
        "description": "Genuine person-to-person mail written by a human to you, "
        "including ongoing conversations and replies.",
    },
    {
        "name": "Shopping",
        "rule": "label_only",
        "description": "Order confirmations, shipping notices, promotions and "
        "offers from shops you buy from.",
    },
    {
        "name": "Travel",
        "rule": "label_only",
        "description": "Bookings, itineraries, boarding passes, check-in "
        "reminders and travel account mail.",
    },
    {
        "name": NEEDS_REVIEW_NAME,
        "rule": "label_only",
        "description": "Reserved: anything the classifier is not confident "
        "about. Stays in the inbox for your decision.",
    },
]

DEFAULT_CATEGORY_NAMES: tuple[str, ...] = tuple(c["name"] for c in DEFAULT_TAXONOMY)


def ensure_default_taxonomy(session: Session, user_id: str) -> int:
    """Seed the defaults for a brand-new user. Returns rows created.

    * User has no categories at all → insert the full default set.
    * User has categories but the reserved "Needs review" row is missing
      (matched by ``is_needs_review``, so renaming it is fine) → restore it.
    * Otherwise → no-op. User edits are never touched.
    """
    from db.models import Category

    existing = session.execute(
        select(Category).where(Category.user_id == user_id)
    ).scalars().all()

    if not existing:
        for position, spec in enumerate(DEFAULT_TAXONOMY):
            session.add(
                Category(
                    user_id=user_id,
                    name=spec["name"],
                    description=spec["description"],
                    rule=spec["rule"],
                    is_needs_review=spec["name"] == NEEDS_REVIEW_NAME,
                    position=position,
                )
            )
        session.flush()
        return len(DEFAULT_TAXONOMY)

    if not any(c.is_needs_review for c in existing):
        spec = DEFAULT_TAXONOMY[-1]
        session.add(
            Category(
                user_id=user_id,
                name=_free_name(existing, NEEDS_REVIEW_NAME),
                description=spec["description"],
                rule="label_only",
                is_needs_review=True,
                position=max(c.position for c in existing) + 1,
            )
        )
        session.flush()
        return 1

    return 0


def _free_name(existing, wanted: str) -> str:
    """A name not colliding with (user_id, name) uniqueness."""
    taken = {c.name.lower() for c in existing}
    name = wanted
    suffix = 2
    while name.lower() in taken:
        name = f"{wanted} {suffix}"
        suffix += 1
    return name
