"""Google OAuth sign-in — ONE consent grants sign-in + ``gmail.modify``.

spec/api.md is the contract:

======  ==========================  ==================================================
GET     /auth/google/login          302 → Google consent (openid+email+profile+gmail.modify)
GET     /auth/google/callback       exchange code, upsert user + gmail account, set
                                    session cookie, redirect to ``/``
POST    /api/auth/logout            revoke the current session row, clear the cookie
POST    /api/gmail/disconnect       delete the token row (best-effort revoke at Google)
======  ==========================  ==================================================

There is no separate "connect Gmail" round trip — the UI's reconnect state
appears only after a token failure flips the account to ``needs_reconnect``, and
reconnecting is simply this same login flow again (the callback overwrites the
stored token and restores ``connected``).

Security properties:

* CSRF state + PKCE verifier round-trip inside the **signed** ``zi_oauth_state``
  cookie — never the query string.
* The refresh token is Fernet-encrypted before it reaches the database and is
  never logged or returned by any route.
* Any Google token failure surfaces as the structured ``gmail_reconnect`` error
  (``"Reconnect Gmail to continue."``) — never a traceback.
* No background work is started here: triggers are manual only (roadmap
  principle 5).
"""

from __future__ import annotations

import os
import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from api._common import (
    GMAIL_RECONNECT,
    GMAIL_RECONNECT_MESSAGE,
    error_body,
    ok,
)
from api.session import (
    COOKIE_NAME,
    require_user_id,
    set_session_cookie,
)
from channels.base import ReauthRequired
from channels.gmail.oauth import (
    OAuthConfigError,
    OAuthExchangeError,
    OAuthResult,
    build_authorization_url,
    exchange_code,
    google_oauth_config,
    revoke_refresh_token,
)
from channels.gmail.store import SqlConnectionStore
from security.crypto import TokenCipher

__all__ = [
    "router",
    "GMAIL_RECONNECT",
    "gmail_reconnect_response",
    "mark_needs_reconnect",
    "get_connection_store",
    "get_code_exchanger",
    "SESSION_COOKIE",
]

router = APIRouter(tags=["auth"])

SESSION_COOKIE = COOKIE_NAME
STATE_COOKIE = "zi_oauth_state"
STATE_SALT = "zi-oauth-state"
STATE_MAX_AGE = 60 * 10
DASHBOARD_URL = os.environ.get("AGENT_DASHBOARD_URL", "http://localhost:3000/")

def gmail_reconnect_response(user_id: str | None = None) -> JSONResponse:
    """The structured ``gmail_reconnect`` error every surface returns on a
    revoked/expired Google token. Also flips the stored connection to
    ``needs_reconnect`` so the dashboard banner is consistent app-wide.
    """
    if user_id:
        mark_needs_reconnect(user_id)
    return JSONResponse(
        status_code=409,
        content=error_body(GMAIL_RECONNECT, GMAIL_RECONNECT_MESSAGE),
    )


def mark_needs_reconnect(user_id: str) -> bool:
    """Flip the user's Gmail connection to ``needs_reconnect``. Never raises."""
    try:
        return SqlConnectionStore().mark_needs_reconnect(user_id)
    except Exception:  # noqa: BLE001 — the error response must still go out
        return False


# --- dependencies ------------------------------------------------------


def get_connection_store() -> SqlConnectionStore:
    return SqlConnectionStore()


def get_code_exchanger():
    """Returns ``(code, state, code_verifier) -> OAuthResult``."""

    def _exchange(code: str, state: str | None, code_verifier: str | None = None) -> OAuthResult:
        return exchange_code(google_oauth_config(), code, state, code_verifier)

    return _exchange


# --- helpers -----------------------------------------------------------


def _error(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=error_body(code, message))


def _secure_cookie(request: Request) -> bool:
    return request.url.scheme == "https"


def _serializer() -> URLSafeTimedSerializer:
    from config.settings import require_secret_key

    return URLSafeTimedSerializer(require_secret_key(), salt=STATE_SALT)


def _read_state_cookie(request: Request) -> dict | None:
    signed = request.cookies.get(STATE_COOKIE)
    if not signed:
        return None
    try:
        payload = _serializer().loads(signed, max_age=STATE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    return payload if isinstance(payload, dict) else None


def _state_is_valid(request: Request, state: str | None) -> bool:
    payload = _read_state_cookie(request)
    if not payload or not state:
        return False
    return secrets.compare_digest(str(payload.get("state", "")), state)


# --- routes ------------------------------------------------------------


@router.get("/auth/google/login")
def google_login(request: Request):
    """Redirect to the Google consent screen — sign-in and Gmail in one grant."""
    try:
        config = google_oauth_config()
    except OAuthConfigError as exc:
        return _error("validation_error", str(exc), 422)

    state = secrets.token_urlsafe(24)
    url, code_verifier = build_authorization_url(config, state=state)

    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        STATE_COOKIE,
        # CSRF state + PKCE verifier travel in the same signed, httponly cookie:
        # the callback builds a different Flow object and needs the verifier,
        # and read from the query string either value would be attacker-controlled.
        _serializer().dumps({"state": state, "code_verifier": code_verifier}),
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
        # the dashboard shows the reason plus a retry button.
        return RedirectResponse(f"{DASHBOARD_URL}?auth_error={error}", status_code=302)

    if not _state_is_valid(request, state):
        return _error(
            "invalid_state",
            "The sign-in link expired or did not originate here. "
            "Start again from Sign in with Google.",
            400,
        )
    if not code:
        return _error("validation_error", "Missing authorization code", 422)

    verifier = (_read_state_cookie(request) or {}).get("code_verifier") or None

    try:
        result: OAuthResult = exchanger(code, state, verifier)
    except ReauthRequired:
        return _error(GMAIL_RECONNECT, GMAIL_RECONNECT_MESSAGE, 409)
    except OAuthExchangeError:
        return _error(
            "provider_error", "Google could not complete the sign-in. Please retry.", 502
        )

    if not result.refresh_token:
        return _error(
            GMAIL_RECONNECT,
            "Google did not return a refresh token. Remove this app at "
            "myaccount.google.com/permissions and sign in again.",
            409,
        )
    if not result.account_email:
        return _error(
            "provider_error", "Could not read the Gmail address for this account.", 502
        )

    user_id, _account_id = store.upsert_user_and_connection(
        email=result.account_email,
        name=result.display_name or result.account_email.split("@")[0],
        picture_url=result.picture_url,
        refresh_token_encrypted=TokenCipher().encrypt(result.refresh_token),
    )

    response = RedirectResponse(DASHBOARD_URL, status_code=302)
    set_session_cookie(response, user_id, request=request)
    response.delete_cookie(STATE_COOKIE, path="/")
    return response


@router.post("/api/auth/logout")
def logout(request: Request):
    """Clear the session cookie (spec/api.md: POST /api/auth/logout)."""
    response = JSONResponse(ok({"logged_out": True}))
    response.delete_cookie(COOKIE_NAME, path="/")
    response.delete_cookie(STATE_COOKIE, path="/")
    return response


@router.post("/api/gmail/disconnect")
def gmail_disconnect(
    user_id: str = Depends(require_user_id),
    store: SqlConnectionStore = Depends(get_connection_store),
):
    """Delete the token row. Best-effort revocation at Google; local delete always."""
    plaintext = store.disconnect(user_id)
    if plaintext:
        revoke_refresh_token(plaintext)  # never raises; local row already gone
    return ok({"disconnected": plaintext is not None})
