"""Inbox mini-audit API (spec/api.md § Audit): fast, read-only, observable.

* ``POST /api/audit``       — run the mini-audit (counts, oldest, top senders,
  tab mix) via :func:`channels.gmail.audit.collect_inbox_snapshot`, persist the
  snapshot, return it. Strictly read-only — this path never constructs a
  mutator and issues zero Gmail writes.
* ``GET  /api/audit/latest`` — the latest snapshot, or ``null``.

A revoked token flips the account to ``needs_reconnect`` and surfaces the
structured ``gmail_reconnect`` state; any other Gmail failure is a retryable
``provider_error`` sentence — never a traceback.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from api._common import PROVIDER_ERROR, api_error, gmail_reconnect, iso, ok
from api.runs import require_current_user
from channels.base import ChannelError, ReauthRequired
from db.models import GmailAccount, InboxSnapshot
from db.session import get_session
from domain.enums import GmailAccountStatus

_log = logging.getLogger("zero_inbox.api.audit")

router = APIRouter()


def _snapshot_payload(row: InboxSnapshot) -> dict:
    return {
        "id": row.id,
        "total_inbox_threads": row.total_inbox_threads,
        "unread": row.unread,
        "oldest_days": row.oldest_days,
        "top_senders": row.top_senders_json or [],
        "category_tab_counts": row.category_tab_counts_json or {},
        "approximate": True,
        "created_at": iso(row.created_at),
    }


def gmail_adapter_for_user(session: Session, user_id: str):
    """A read-only :class:`GmailAdapter` for the user's connected mailbox."""
    from channels.gmail.adapter import GmailAdapter
    from security.crypto import TokenCipher

    account = session.execute(
        select(GmailAccount).where(GmailAccount.user_id == user_id)
    ).scalar_one_or_none()
    if account is None or account.status != GmailAccountStatus.CONNECTED:
        raise gmail_reconnect()
    try:
        refresh_token = TokenCipher().decrypt(account.refresh_token_encrypted)
        return GmailAdapter.for_refresh_token(
            refresh_token, user_id=user_id, account_email=account.google_email
        )
    except Exception as exc:  # noqa: BLE001 — a broken token == reconnect
        _log.warning("audit.gmail_client_failed user_id=%s error=%r", user_id, exc)
        raise gmail_reconnect() from exc


#: Newest-thread sample fed to the top-senders slice (and the fallback totals).
AUDIT_SAMPLE_SIZE = 100


def collect_snapshot(session: Session, user_id: str) -> dict:
    """The mini-audit data, entered through THE thread-listing read seam.

    The listing (``channels.gmail.client.list_inbox_threads``) is the fakeable
    seam — its errors (revoked token → ``ReauthRequired``) propagate to the
    route. The richer Gmail estimates are then attempted on the real adapter;
    if they are unavailable after a successful listing, the snapshot degrades
    to the sampled listing rather than failing the audit.
    """
    from channels.gmail import client
    from channels.gmail.audit import collect_inbox_snapshot, snapshot_from_views

    views = client.list_inbox_threads(user_id, limit=AUDIT_SAMPLE_SIZE)
    try:
        adapter = gmail_adapter_for_user(session, user_id)
        return collect_inbox_snapshot(adapter, sample_size=AUDIT_SAMPLE_SIZE)
    except ReauthRequired:
        raise
    except Exception as exc:  # noqa: BLE001 — degrade, listing already succeeded
        _log.warning(
            "audit.estimates_unavailable user_id=%s error=%r — using sampled listing",
            user_id,
            exc,
        )
        return snapshot_from_views(views)


@router.post("/api/audit")
def run_audit(
    user_id: str = Depends(require_current_user),
    session: Session = Depends(get_session),
) -> dict:
    try:
        data = collect_snapshot(session, user_id)
    except ReauthRequired as exc:
        session.execute(
            update(GmailAccount)
            .where(GmailAccount.user_id == user_id)
            .values(status=GmailAccountStatus.NEEDS_RECONNECT)
        )
        session.commit()
        raise gmail_reconnect() from exc
    except ChannelError as exc:
        _log.warning("audit.failed user_id=%s error=%r", user_id, exc)
        raise api_error(
            PROVIDER_ERROR, "The inbox audit failed — try again in a moment.", 502
        ) from exc

    row = InboxSnapshot(
        user_id=user_id,
        total_inbox_threads=int(data.get("total_inbox_threads") or 0),
        unread=int(data.get("unread") or 0),
        oldest_days=int(data.get("oldest_days") or 0),
        top_senders_json=list(data.get("top_senders") or []),
        category_tab_counts_json=dict(data.get("category_tab_counts") or {}),
    )
    session.add(row)
    session.commit()
    return ok(_snapshot_payload(row))


@router.get("/api/audit/latest")
def latest_audit(
    user_id: str = Depends(require_current_user),
    session: Session = Depends(get_session),
) -> dict:
    row = session.execute(
        select(InboxSnapshot)
        .where(InboxSnapshot.user_id == user_id)
        .order_by(InboxSnapshot.created_at.desc(), InboxSnapshot.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return ok(_snapshot_payload(row) if row else None)
