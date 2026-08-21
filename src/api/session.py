"""Signed session cookie + the current-user surface (``GET /api/me``).

The session cookie (``zi_session``) carries ``{"uid": <user_id>}``, signed with
``AGENT_SECRET_KEY`` via itsdangerous — which is **required**; there is no dev
fallback key, because a published constant would make every session forgeable.

Every ``/api/*`` route depends on :func:`require_user_id`, the single user-scope
chokepoint: a route can only ever see rows for the signed-in user. An absent,
tampered, or expired cookie is ``401 {code:"signed_out"}`` (spec/api.md).

``api/auth.py`` (Google OAuth) calls :func:`set_session_cookie` after it upserts
the user, and :func:`clear_session_cookie` on logout.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from api._common import SIGNED_OUT, api_error, iso, ok
from db.session import get_session

COOKIE_NAME = "zi_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
_SALT = "zi-session-v1"

router = APIRouter()


def _secret_key() -> str:
    """The signing key, or the one canonical fatal-config error (settings.py)."""
    from config.settings import require_secret_key

    return require_secret_key()


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_secret_key(), salt=_SALT)


def issue_session_token(user_id: str) -> str:
    """Mint the signed cookie value."""
    return _serializer().dumps({"uid": user_id})


def issue_session_cookie(user_id: str) -> str:
    """Session-cookie contract (tests/conftest.py): the signed cookie value."""
    return issue_session_token(user_id)


def read_session_token(token: str) -> str | None:
    """Return the verified user id, or None for absent/tampered/expired."""
    try:
        payload = _serializer().loads(token, max_age=COOKIE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(payload, dict):
        return None
    uid = payload.get("uid")
    return uid if isinstance(uid, str) and uid else None


def _is_https(request: Request | None) -> bool:
    """True when this request arrived over TLS (scheme, with raw-scope fallback)."""
    if request is None:
        return False
    try:
        if request.url.scheme == "https":
            return True
    except Exception:  # noqa: BLE001 — fall through to the scope
        pass
    return request.scope.get("scheme") == "https"


def set_session_cookie(
    response: Response, user_id: str, *, request: Request | None = None
) -> None:
    response.set_cookie(
        COOKIE_NAME,
        issue_session_token(user_id),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        # `secure` follows the scheme so the cookie is never sent in clear
        # over an https deployment.
        secure=_is_https(request),
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def optional_user_id(request: Request) -> str | None:
    """Cookie read for logging context. Not an authorization decision."""
    token = request.cookies.get(COOKIE_NAME)
    return read_session_token(token) if token else None


def require_user_id(request: Request) -> str:
    """FastAPI dependency — the user-scope guard for every /api/* route."""
    token = request.cookies.get(COOKIE_NAME)
    uid = read_session_token(token) if token else None
    if uid is None:
        raise api_error(SIGNED_OUT, "Sign in with Google to continue.", 401)
    return uid


@router.get("/api/me")
def me(
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    """spec/api.md: user + gmail connection status (connected/needs_reconnect/none).

    Scoped to the session's user — another user's connection is unreachable here.
    """
    from db.models import GmailAccount, User

    user = session.get(User, user_id)
    if user is None:
        raise api_error(SIGNED_OUT, "Session refers to an unknown user.", 401)

    account = session.execute(
        select(GmailAccount).where(GmailAccount.user_id == user_id)
    ).scalars().first()

    gmail = (
        {"status": "none", "email": None, "connected_at": None}
        if account is None
        else {
            "status": account.status,  # connected | needs_reconnect
            "email": account.google_email,
            "connected_at": iso(account.connected_at),
        }
    )

    return ok(
        {
            "user": {
                "id": user.id,
                "email": user.email,
                "name": user.name or user.email.split("@")[0],
                "picture_url": user.picture_url,
            },
            "gmail": gmail,
        }
    )
