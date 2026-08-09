"""Per-user memory: VIP list, priorities profile, and correction signal.

Pure DB-layer helpers used by ``api/memory.py``. Phase 2 stops at recording the
correction signal — **no function in this module ever writes a ``rules`` row.**
Turning a pattern of corrections into a proposed rule is Phase 3's rule miner,
which later reads the ``corrections`` table these helpers append to; see
spec/capabilities/rule-proposals.md and spec/capabilities/user-memory.md.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Correction, Decision, Item, PriorityProfile, SenderProfile, VipEntry

VALID_VIP_KINDS = ("email", "domain", "keyword")

#: How much a single correction raises the corrected sender's importance score.
#: Clamped to [0, 1]; un-archiving is treated as the strongest signal.
CORRECTION_IMPORTANCE_BUMP = 0.2


def add_vip_entry(session: Session, user_id: str, kind: str, value: str) -> VipEntry:
    if kind not in VALID_VIP_KINDS:
        raise ValueError(f"kind must be one of {VALID_VIP_KINDS}, got {kind!r}")
    value = value.strip()
    if not value:
        raise ValueError("value must not be empty")

    existing = session.execute(
        select(VipEntry).where(
            VipEntry.user_id == user_id,
            VipEntry.kind == kind,
            VipEntry.value == value,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    entry = VipEntry(user_id=user_id, kind=kind, value=value)
    session.add(entry)
    session.flush()
    return entry


def list_vip_entries(session: Session, user_id: str) -> list[VipEntry]:
    return list(
        session.execute(
            select(VipEntry).where(VipEntry.user_id == user_id).order_by(VipEntry.created_at)
        ).scalars()
    )


def remove_vip_entry(session: Session, user_id: str, vip_entry_id: str) -> bool:
    entry = session.get(VipEntry, vip_entry_id)
    if entry is None or entry.user_id != user_id:
        return False
    session.delete(entry)
    session.flush()
    return True


def is_vip(session: Session, user_id: str, *, email: str = "", domain: str = "", subject: str = "") -> bool:
    """True iff email/domain/keyword matches any VIP entry — never archivable."""
    entries = list_vip_entries(session, user_id)
    email_l = (email or "").lower()
    domain_l = (domain or "").lower()
    subject_l = (subject or "").lower()
    for entry in entries:
        val = entry.value.lower()
        if entry.kind == "email" and val == email_l:
            return True
        if entry.kind == "domain" and (domain_l == val or domain_l.endswith("." + val)):
            return True
        if entry.kind == "keyword" and val and val in subject_l:
            return True
    return False


def get_priority_profile(session: Session, user_id: str) -> str:
    row = session.get(PriorityProfile, user_id)
    return row.text if row is not None else ""


def set_priority_profile(session: Session, user_id: str, text: str) -> PriorityProfile:
    row = session.get(PriorityProfile, user_id)
    if row is None:
        row = PriorityProfile(user_id=user_id, text=text)
        session.add(row)
    else:
        row.text = text
    session.flush()
    return row


def list_corrections(session: Session, user_id: str) -> list[Correction]:
    return list(
        session.execute(
            select(Correction)
            .where(Correction.user_id == user_id)
            .order_by(Correction.created_at.desc())
        ).scalars()
    )


def record_correction(
    session: Session,
    user_id: str,
    *,
    item_id: str,
    from_action: str,
    to_action: str,
    source: str = "dashboard",
    decision_id: str | None = None,
    note: str | None = None,
) -> Correction:
    """Append a correction row and raise the sender's importance score.

    Phase 2: signal only. This function never inserts, updates, or otherwise
    touches the ``rules`` table — it feeds the ``corrections`` table that
    Phase 3's rule miner reads later.
    """
    item = session.get(Item, item_id)
    if item is None or item.user_id != user_id:
        raise ValueError("item not found for this user")

    correction = Correction(
        user_id=user_id,
        item_id=item_id,
        decision_id=decision_id,
        from_action=from_action,
        to_action=to_action,
        source=source,
        note=note,
    )
    session.add(correction)
    session.flush()

    _bump_sender_importance(session, user_id, item)

    return correction


def _bump_sender_importance(session: Session, user_id: str, item: Item) -> SenderProfile:
    sender_email = (item.from_email or "").lower()
    profile = session.execute(
        select(SenderProfile).where(
            SenderProfile.user_id == user_id, SenderProfile.sender_email == sender_email
        )
    ).scalar_one_or_none()

    now = datetime.now(timezone.utc)
    if profile is None:
        profile = SenderProfile(
            user_id=user_id,
            sender_email=sender_email,
            sender_domain=item.from_domain or "",
            importance_score=min(1.0, CORRECTION_IMPORTANCE_BUMP),
            last_seen_at=now,
        )
        session.add(profile)
    else:
        profile.importance_score = min(1.0, profile.importance_score + CORRECTION_IMPORTANCE_BUMP)
        profile.last_seen_at = now

    session.flush()
    return profile
