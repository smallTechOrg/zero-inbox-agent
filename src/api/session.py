"""Signed session cookie + the current-user surface.

The session cookie (``zi_session``) carries nothing but the user id, signed with
``AGENT_SECRET_KEY`` via itsdangerous. Every ``/api/*`` route depends on
:func:`require_user_id`, so a route can only ever see rows for the signed-in user.

``api/auth.py`` (Google OAuth) calls :func:`set_session_cookie` after it upserts the
user, and :func:`clear_session_cookie` on logout.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from api._common import UNAUTHENTICATED, api_error, iso, ok
from db.session import get_session

COOKIE_NAME = "zi_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
_SALT = "zi-session-v1"

router = APIRouter()


def _secret_key() -> str:
    """Read the signing key from settings, falling back to the raw env var.

    ``secret_key`` is declared in ``src/config/settings.py``; the env fallback keeps the
    module importable if settings are loaded in a bare context (e.g. a script).
    """
    from config.settings import get_settings

    key = getattr(get_settings(), "secret_key", "") or os.environ.get("AGENT_SECRET_KEY", "")
    if not key:
        raise api_error(
            "validation_error",
            "AGENT_SECRET_KEY is not set — sessions cannot be signed.",
            500,
        )
    return key


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_secret_key(), salt=_SALT)


def issue_session_token(user_id: str) -> str:
    return _serializer().dumps({"uid": user_id})


def read_session_token(token: str) -> str | None:
    try:
        payload = _serializer().loads(token, max_age=COOKIE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(payload, dict):
        return None
    uid = payload.get("uid")
    return uid if isinstance(uid, str) and uid else None


def set_session_cookie(response: Response, user_id: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        issue_session_token(user_id),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=False,  # local-first tool served over http://localhost:8001
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def optional_user_id(request: Request) -> str | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return read_session_token(token)


def require_user_id(request: Request) -> str:
    """FastAPI dependency — the user-scope guard for every /api/* route."""
    user_id = optional_user_id(request)
    if user_id is None:
        raise api_error(UNAUTHENTICATED, "Sign in with Google to continue.")
    return user_id


@router.get("/api/me")
def me(
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    from db.models import ChannelAccount, User, UserSettings

    user = session.get(User, user_id)
    if user is None:
        raise api_error(UNAUTHENTICATED, "Session refers to an unknown user.")

    accounts = session.execute(
        select(ChannelAccount)
        .where(ChannelAccount.user_id == user_id)
        .order_by(ChannelAccount.connected_at)
    ).scalars().all()

    settings_row = session.get(UserSettings, user_id)

    return ok(
        {
            "user": {
                "id": user.id,
                "email": user.email,
                "display_name": user.display_name,
            },
            "connections": [
                {
                    "id": a.id,
                    "channel": a.channel,
                    "account_email": a.account_email,
                    "status": a.status,
                    "connected_at": iso(a.connected_at),
                }
                for a in accounts
            ],
            "settings": _settings_payload(settings_row),
        }
    )


def _settings_payload(row) -> dict:
    """Settings are returned with Phase-1 defaults when the row does not exist yet.

    ``dry_run`` is always reported true in Phase 1 — the client cannot turn it off.
    """
    if row is None:
        return {
            "auto_act_threshold": 0.95,
            "confidence_floor": 0.75,
            "dry_run": True,
            "llm_model": "",
            "digest_hour_local": 8,
            "timezone": "UTC",
        }
    return {
        "auto_act_threshold": row.auto_act_threshold,
        "confidence_floor": row.confidence_floor,
        "dry_run": True,
        "llm_model": row.llm_model,
        "digest_hour_local": row.digest_hour_local,
        "timezone": row.timezone,
    }
