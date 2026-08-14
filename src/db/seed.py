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

#: Phase 7 per-category autonomy bars (spec/capabilities/drive-to-inbox-zero.md § B).
#: Every other category is seeded NULL and inherits the global
#: ``user_settings.auto_act_threshold`` (0.80), which is what keeps the global
#: slider load-bearing.
#:
#: - ``outreach`` is the one archive-by-default category where a false archive
#:   costs a real opportunity (a genuine intro reads like recruiter spam), so it
#:   sits one measured confidence band above the global bar.
#: - ``receipts`` is ``default_action = archive`` (Phase 7 — keeping it meant a
#:   permanent ~715-thread inbox floor, so "inbox zero" could never be true), and
#:   its bar is therefore **live**, not inert. Financial records earn the same
#:   one-band margin as outreach: a wrongly-archived invoice is far more costly
#:   than a wrongly-kept one, and the never-miss layer still holds anything
#:   ``time_sensitive`` regardless of this bar.
SEEDED_AUTO_ACT_THRESHOLDS: dict[str, float] = {
    "outreach": 0.85,
    "receipts": 0.85,
}


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
                auto_act_threshold=SEEDED_AUTO_ACT_THRESHOLDS.get(spec["key"]),
                is_default=True,
                sort_order=order,
            )
        )
        created += 1
    if created:
        session.flush()
    return created
