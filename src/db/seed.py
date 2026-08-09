"""Idempotent seed data — the six default categories every user starts with.

spec/capabilities/taxonomy-management.md: "A new user is seeded with the six
default categories." Seeding runs at OAuth-connect time (``channels/gmail/store``)
and again defensively before every triage run (``graph/persistence``), so an
existing user whose taxonomy was never materialised is backfilled on their next
run — without ever creating duplicate rows (unique on ``(user_id, key)``).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from tools.rules import DEFAULT_TAXONOMY


def ensure_default_taxonomy(session: Session, user_id: str) -> int:
    """Insert any missing default categories for ``user_id``. Returns rows created.

    Idempotent by construction: only keys absent from the user's taxonomy are
    inserted, so user-created and user-edited categories are never touched.
    """
    from db.models import Category

    existing = {
        row.key
        for row in session.execute(
            select(Category).where(Category.user_id == user_id)
        ).scalars()
    }

    created = 0
    for order, spec in enumerate(DEFAULT_TAXONOMY):
        if spec["key"] in existing:
            continue
        session.add(
            Category(
                user_id=user_id,
                key=spec["key"],
                name=spec["name"],
                description=spec["description"],
                channel_label_name=f"ZeroInbox/{spec['name']}",
                default_action=spec["default_action"],
                is_default=True,
                sort_order=order,
            )
        )
        created += 1
    if created:
        session.flush()
    return created
