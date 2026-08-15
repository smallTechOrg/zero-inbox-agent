"""Account & security — the product around the already-multi-tenant data model.

Five routes (spec/api.md#routes):

* ``GET    /api/account``                          identity, mailboxes, devices, counts
* ``DELETE /api/account/connections/{id}``         disconnect a mailbox
* ``DELETE /api/account/sessions/{id}``            sign out one device
* ``POST   /api/account/sessions/revoke-all``      sign out everywhere
* ``DELETE /api/account``                          delete the account (cascade)

Three things about this module are load-bearing and none of them are incidental:

1. **Zero Gmail operations.** Disconnecting a mailbox and deleting an account
   perform no mailbox mutation of any kind — archived mail stays archived,
   labels stay put, nothing is deleted. The only external call here is Google's
   OAuth *revoke* endpoint, which touches no message.
2. **Nothing sensitive is ever returned.** No ``refresh_token_enc``, no raw IP,
   no raw user-agent string appears in any response from any route below.
3. **Every query is user-scoped** and another user's row is a ``404``, never a
   ``403`` — matching the existing cross-user isolation tests.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api._common import VALIDATION_ERROR, api_error, iso, not_found, ok
from api.session import (
    clear_session_cookie,
    current_session_id,
    require_user_id,
)
from db.session import get_session
from observability.logging import get_logger

router = APIRouter(tags=["account"])

_log = get_logger("account")


class DeleteAccountRequest(BaseModel):
    confirm_email: str = ""


def _user_scoped_tables():
    """Every table carrying a ``user_id``, in a delete-safe order (children first).

    Derived from the metadata rather than hand-listed: a hand-written delete loop
    is exactly the thing that silently misses a table added two phases later.
    """
    from db.models import Base

    return [
        table
        for table in reversed(Base.metadata.sorted_tables)
        if "user_id" in table.c and table.name != "users"
    ]


# --- GET /api/account ---------------------------------------------------


@router.get("/api/account")
def get_account(
    request: Request,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    from db.models import ActionLog, ChannelAccount, Decision, User, UserSession

    user = session.get(User, user_id)
    if user is None:
        raise not_found("Account")

    connections = (
        session.execute(
            select(ChannelAccount)
            .where(ChannelAccount.user_id == user_id)
            .order_by(ChannelAccount.connected_at)
        )
        .scalars()
        .all()
    )

    sessions = (
        session.execute(
            select(UserSession)
            .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
            .order_by(UserSession.created_at)
        )
        .scalars()
        .all()
    )
    active_sid = current_session_id(request)

    def _count(model) -> int:
        return int(
            session.execute(
                select(func.count(model.id)).where(model.user_id == user_id)
            ).scalar_one()
            or 0
        )

    return ok(
        {
            "user": {
                "id": user.id,
                "email": user.email,
                "display_name": user.display_name,
                "created_at": iso(user.created_at),
            },
            # NOTE: `refresh_token_enc` and `scopes` are deliberately absent.
            "connections": [
                {
                    "id": c.id,
                    "channel": c.channel,
                    "account_email": c.account_email,
                    "status": c.status,
                    "connected_at": iso(c.connected_at),
                    "last_synced_at": iso(c.last_synced_at),
                }
                for c in connections
            ],
            # NOTE: only the DERIVED user-agent summary. No raw UA, no IP, no ip_hash.
            "sessions": [
                {
                    "id": s.id,
                    "created_at": iso(s.created_at),
                    "last_seen_at": iso(s.last_seen_at),
                    "user_agent_summary": s.user_agent_summary,
                    "current": s.id == active_sid,
                }
                for s in sessions
            ],
            "counts": {
                "decisions": _count(Decision),
                "action_logs": _count(ActionLog),
                "connections": len(connections),
            },
        }
    )


# --- DELETE /api/account/connections/{connection_id} --------------------


@router.delete("/api/account/connections/{connection_id}")
def disconnect_connection(
    connection_id: str,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    """Revoke at Google (best effort), then delete the local ciphertext.

    **Zero Gmail operations.** Triage history is retained: the decisions the
    agent already made are the user's audit trail, not the mailbox's.
    """
    from db.models import ChannelAccount

    account = session.get(ChannelAccount, connection_id)
    if account is None or account.user_id != user_id:
        raise not_found("Connection")

    ciphertext = account.refresh_token_enc
    if ciphertext:
        # Best effort by contract: a Google outage must never leave a stored
        # token we cannot remove, so the local row goes regardless.
        try:
            from channels.gmail.oauth import revoke_refresh_token
            from security.crypto import TokenCipher

            revoked = revoke_refresh_token(TokenCipher().decrypt(ciphertext))
            if not revoked:
                _log.warning(
                    "connection_revoke_incomplete", connection_id=connection_id
                )
        except Exception as exc:  # noqa: BLE001 — never blocks the disconnect
            _log.warning(
                "connection_revoke_failed",
                connection_id=connection_id,
                cause=repr(exc),
            )

    session.delete(account)
    session.commit()
    return ok({"disconnected": True})


# --- sessions -----------------------------------------------------------


@router.delete("/api/account/sessions/{session_id}")
def revoke_one_session(
    session_id: str,
    response: Response,
    request: Request,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    from datetime import datetime, timezone

    from db.models import UserSession

    row = session.get(UserSession, session_id)
    if row is None or row.user_id != user_id:
        raise not_found("Session")

    if row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
    session.commit()

    if session_id == current_session_id(request):
        clear_session_cookie(response)
    return ok({"revoked": True})


@router.post("/api/account/sessions/revoke-all")
def revoke_all(
    response: Response,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    """Sign out everywhere — including this device. A session you can see is a
    session you can revoke.

    The revoke runs on the **injected** session: a second connection to the same
    SQLite file is a lock-contention shape this codebase avoids everywhere else.
    """
    from datetime import datetime, timezone

    from db.models import UserSession

    now = datetime.now(timezone.utc)
    rows = (
        session.query(UserSession)
        .filter(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .all()
    )
    for row in rows:
        row.revoked_at = now
    session.commit()
    revoked = len(rows)
    clear_session_cookie(response)
    return ok({"revoked": revoked})


# --- DELETE /api/account ------------------------------------------------


@router.delete("/api/account")
def delete_account(
    body: DeleteAccountRequest,
    response: Response,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    """Cascade-delete every user-scoped row. **Zero Gmail calls.**

    Deleting the account does not un-archive, un-label or delete a single
    message — the confirm dialog says so, and this route makes it true.
    """
    from db.models import User

    user = session.get(User, user_id)
    if user is None:
        raise not_found("Account")

    if (body.confirm_email or "").strip().lower() != (user.email or "").lower():
        # Nothing is deleted. The typed address is the confirmation.
        raise api_error(
            VALIDATION_ERROR,
            "Type your account email exactly to confirm deletion. Nothing was deleted.",
        )

    # SQLite does not enforce ON DELETE CASCADE unless PRAGMA foreign_keys is on,
    # so relying on the FKs alone would orphan rows here while working on
    # Postgres. Driving the delete off the metadata deletes every user-scoped
    # table on every dialect and cannot miss a table added later.
    for table in _user_scoped_tables():
        session.execute(table.delete().where(table.c.user_id == user_id))
    session.delete(user)
    session.commit()

    clear_session_cookie(response)
    return ok({"deleted": True})
