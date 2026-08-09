"""Google OAuth web flow + dashboard session.

Completing the Gmail OAuth flow both connects the mailbox and establishes the
session — there is no separate login in v1.

Routes
------
GET  /auth/google/start     302 → Google consent (alias: /auth/google/login)
GET  /auth/google/callback  code → tokens → connection + session → 302 /app/
POST /auth/logout           clears the session cookie

Cookie contract (read by `api/session.py`): ``zi_session`` is an itsdangerous
``URLSafeTimedSerializer`` token, salt ``zi-session``, payload ``{"user_id": ...}``,
signed with ``AGENT_SECRET_KEY``.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from channels.base import ReauthRequired
from channels.gmail.oauth import (
    OAuthConfigError,
    OAuthExchangeError,
    OAuthResult,
    build_authorization_url,
    exchange_code,
    google_oauth_config,
)
from channels.gmail.store import SqlConnectionStore
from security.crypto import TokenCipher, get_secret_key

__all__ = [
    "router",
    "get_connection_store",
    "get_code_exchanger",
    "issue_session_cookie",
    "read_session_user_id",
    "OAuthExchangeError",
    "SESSION_COOKIE",
]

router = APIRouter(tags=["auth"])

SESSION_COOKIE = "zi_session"
STATE_COOKIE = "zi_oauth_state"
SESSION_SALT = "zi-session"
STATE_SALT = "zi-oauth-state"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
STATE_MAX_AGE = 60 * 10
DASHBOARD_URL = "/app/"


# --- session cookie ----------------------------------------------------


def _serializer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_secret_key() or "insecure-dev-key", salt=salt)


def issue_session_cookie(user_id: str) -> str:
    return _serializer(SESSION_SALT).dumps({"user_id": user_id})


def read_session_user_id(cookie: str | None, *, max_age: int = SESSION_MAX_AGE) -> str | None:
    """Return the signed-in user id, or None if the cookie is absent/invalid."""
    if not cookie:
        return None
    try:
        payload = _serializer(SESSION_SALT).loads(cookie, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    user_id = (payload or {}).get("user_id")
    return user_id or None


# --- dependencies ------------------------------------------------------


def get_connection_store() -> SqlConnectionStore:
    return SqlConnectionStore()


def get_code_exchanger():
    """Returns `(code, state) -> OAuthResult` against the real Google endpoint."""

    def _exchange(code: str, state: str | None) -> OAuthResult:
        return exchange_code(google_oauth_config(), code, state)

    return _exchange


# --- helpers -----------------------------------------------------------


def _error(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"data": None, "error": {"code": code, "message": message}},
    )


def _secure_cookie(request: Request) -> bool:
    return request.url.scheme == "https"


# --- routes ------------------------------------------------------------


@router.get("/auth/google/start")
@router.get("/auth/google/login")
def google_start(request: Request):
    """Send the user to the real Google consent screen with a CSRF state."""
    try:
        config = google_oauth_config()
    except OAuthConfigError as exc:
        return _error("validation_error", str(exc), 422)

    state = secrets.token_urlsafe(24)
    url = build_authorization_url(config, state=state)

    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        STATE_COOKIE,
        _serializer(STATE_SALT).dumps(state),
        max_age=STATE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_secure_cookie(request),
        path="/",
    )
    return response


@router.get("/auth/google/callback")
def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    store: SqlConnectionStore = Depends(get_connection_store),
    exchanger=Depends(get_code_exchanger),
):
    if error:
        # The user declined consent (or Google refused). Nothing is persisted;
        # the dashboard shows the reason plus a Retry button.
        return RedirectResponse(f"{DASHBOARD_URL}?auth_error={error}", status_code=302)

    if not _state_is_valid(request, state):
        return _error(
            "invalid_state",
            "The sign-in link expired or did not originate here. Start again from Connect Gmail.",
            400,
        )
    if not code:
        return _error("validation_error", "Missing authorization code", 422)

    try:
        result: OAuthResult = exchanger(code, state)
    except ReauthRequired as exc:
        return _error("reauth_required", str(exc), 409)
    except OAuthExchangeError:
        return _error(
            "provider_error", "Google could not complete the sign-in. Please retry.", 502
        )

    if not result.refresh_token:
        return _error(
            "reauth_required",
            "Google did not return a refresh token. Remove this app at "
            "myaccount.google.com/permissions and connect again.",
            409,
        )
    if not result.account_email:
        return _error(
            "provider_error", "Could not read the Gmail address for this account.", 502
        )

    refresh_token_enc = TokenCipher().encrypt(result.refresh_token)
    user_id, _connection_id = store.upsert_user_and_connection(
        email=result.account_email,
        display_name=result.display_name or result.account_email.split("@")[0],
        refresh_token_enc=refresh_token_enc,
        scopes=list(result.scopes),
        channel="gmail",
    )

    response = RedirectResponse(DASHBOARD_URL, status_code=302)
    response.set_cookie(
        SESSION_COOKIE,
        issue_session_cookie(user_id),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_secure_cookie(request),
        path="/",
    )
    response.delete_cookie(STATE_COOKIE, path="/")
    return response


@router.post("/auth/logout")
def logout(request: Request):
    response = JSONResponse({"data": {"logged_out": True}, "error": None})
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(STATE_COOKIE, path="/")
    return response


def _state_is_valid(request: Request, state: str | None) -> bool:
    signed = request.cookies.get(STATE_COOKIE)
    if not signed or not state:
        return False
    try:
        expected = _serializer(STATE_SALT).loads(signed, max_age=STATE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return False
    return secrets.compare_digest(str(expected), state)
