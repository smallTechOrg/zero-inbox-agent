"""VIP list, priorities profile, and corrections — spec/api.md § Phase 2.

Corrections recorded here are **signal only**: Phase 2 never creates, proposes,
or writes a ``rules`` row. See spec/capabilities/user-memory.md.
"""

from __future__ import annotations

from pydantic import BaseModel

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from api._common import VALIDATION_ERROR, api_error, iso, not_found, ok
from api.session import require_user_id
from db.session import get_session
from tools.memory import (
    VALID_VIP_KINDS,
    add_vip_entry,
    get_priority_profile,
    list_corrections,
    list_vip_entries,
    record_correction,
    remove_vip_entry,
    set_priority_profile,
)

router = APIRouter()


class VipCreate(BaseModel):
    kind: str
    value: str


class ProfileUpdate(BaseModel):
    text: str


class CorrectionCreate(BaseModel):
    item_id: str
    from_action: str
    to_action: str
    source: str = "dashboard"
    decision_id: str | None = None
    note: str | None = None


def _vip_payload(entry) -> dict:
    return {
        "id": entry.id,
        "kind": entry.kind,
        "value": entry.value,
        "created_at": iso(entry.created_at),
    }


def _correction_payload(c) -> dict:
    return {
        "id": c.id,
        "item_id": c.item_id,
        "decision_id": c.decision_id,
        "from_action": c.from_action,
        "to_action": c.to_action,
        "source": c.source,
        "note": c.note,
        "created_at": iso(c.created_at),
    }


@router.get("/api/vip")
def list_vip(
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    return ok([_vip_payload(e) for e in list_vip_entries(session, user_id)])


@router.post("/api/vip")
def create_vip(
    body: VipCreate,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    if body.kind not in VALID_VIP_KINDS:
        raise api_error(VALIDATION_ERROR, f"kind must be one of {list(VALID_VIP_KINDS)}")
    if not body.value or not body.value.strip():
        raise api_error(VALIDATION_ERROR, "value must not be empty")
    entry = add_vip_entry(session, user_id, body.kind, body.value)
    return ok(_vip_payload(entry))


@router.delete("/api/vip/{vip_entry_id}")
def delete_vip(
    vip_entry_id: str,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    removed = remove_vip_entry(session, user_id, vip_entry_id)
    if not removed:
        raise not_found("VIP entry")
    return ok({"deleted": True})


@router.get("/api/profile")
def get_profile(
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    return ok({"text": get_priority_profile(session, user_id)})


@router.put("/api/profile")
def put_profile(
    body: ProfileUpdate,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    row = set_priority_profile(session, user_id, body.text)
    return ok({"text": row.text})


@router.get("/api/corrections")
def get_corrections(
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    return ok([_correction_payload(c) for c in list_corrections(session, user_id)])


@router.post("/api/corrections")
def post_correction(
    body: CorrectionCreate,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    try:
        correction = record_correction(
            session,
            user_id,
            item_id=body.item_id,
            from_action=body.from_action,
            to_action=body.to_action,
            source=body.source,
            decision_id=body.decision_id,
            note=body.note,
        )
    except ValueError as exc:
        raise api_error(VALIDATION_ERROR, str(exc)) from exc
    return ok(_correction_payload(correction))
