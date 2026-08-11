"""Google OAuth web flow + dashboard session.

Completing the Gmail OAuth flow both connects the mailbox and establishes the
session — there is no separate login in v1.

Routes
------
GET  /auth/google/start     302 → Google consent
GET  /auth/google/callback  code → tokens → connection + session → 302 /app/
POST /auth/logout           clears the session cookie

Cookie contract (read by `api/session.py`): ``zi_session`` is an itsdangerous
``URLSafeTimedSerializer`` token, salt ``zi-session``, payload ``{"user_id": ...}``,
signed with ``AGENT_SECRET_KEY``.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from api.session import issue_session_token, read_session_token, set_session_cookie
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


# The session cookie is owned by `api/session.py` — it is the only module that
# may mint or read it. This module used its own salt and payload key, so every
# cookie it wrote failed the reader's signature check and /api/me 401'd for a
# user who had in fact just connected successfully. Delegate, never re-implement.
def issue_session_cookie(user_id: str) -> str:
    return issue_session_token(user_id)


def read_session_user_id(cookie: str | None, *, max_age: int = SESSION_MAX_AGE) -> str | None:
    """Return the signed-in user id, or None if the cookie is absent/invalid."""
    if not cookie:
        return None
    return read_session_token(cookie)


# --- dependencies ------------------------------------------------------


def get_connection_store() -> SqlConnectionStore:
    return SqlConnectionStore()


def get_code_exchanger():
    """Returns `(code, state) -> OAuthResult` against the real Google endpoint."""

    def _exchange(
        code: str, state: str | None, code_verifier: str | None = None
    ) -> OAuthResult:
        return exchange_code(google_oauth_config(), code, state, code_verifier)

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
def google_start(request: Request):
    """Send the user to the real Google consent screen with a CSRF state."""
    try:
        config = google_oauth_config()
    except OAuthConfigError as exc:
        return _error("validation_error", str(exc), 422)

    state = secrets.token_urlsafe(24)
    url, code_verifier = build_authorization_url(config, state=state)

    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        STATE_COOKIE,
        # The PKCE verifier rides along in the same signed, httponly cookie as
        # the CSRF state — it must reach the callback to complete the exchange.
        _serializer(STATE_SALT).dumps({"state": state, "code_verifier": code_verifier}),
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
    background_tasks: BackgroundTasks,
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

    verifier = (_read_state_cookie(request) or {}).get("code_verifier") or None

    try:
        result: OAuthResult = exchanger(code, state, verifier)
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
    user_id, connection_id = store.upsert_user_and_connection(
        email=result.account_email,
        display_name=result.display_name or result.account_email.split("@")[0],
        refresh_token_enc=refresh_token_enc,
        scopes=list(result.scopes),
        channel="gmail",
    )

    background_tasks.add_task(_auto_triage_task, user_id=user_id, connection_id=connection_id)

    response = RedirectResponse(DASHBOARD_URL, status_code=302)
    set_session_cookie(response, user_id)
    response.delete_cookie(STATE_COOKIE, path="/")
    return response


def _auto_triage_task(*, user_id: str, connection_id: str) -> None:
    """Auto-trigger a full triage run after OAuth connect. Never raises."""
    import structlog

    log = structlog.get_logger("auth")
    log.info("auto_triage_triggered", user_id=user_id, connection_id=connection_id)
    try:
        from datetime import datetime, timezone
        from uuid import uuid4

        from db.models import TriageRun
        from db.session import create_db_session
        from graph.runner import run_triage

        run_id = str(uuid4())
        with create_db_session() as session:
            run = TriageRun(
                id=run_id,
                user_id=user_id,
                channel_account_id=connection_id,
                kind="incremental",
                status="running",
                dry_run=False,
                items_total=0,
                items_decided=0,
                counts={},
                started_at=datetime.now(timezone.utc),
            )
            session.add(run)

        run_triage(
            user_id=user_id,
            channel_account_id=connection_id,
            limit=10_000,
            dry_run=False,
            run_id=run_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("auto_triage_failed", user_id=user_id, connection_id=connection_id, error=str(exc))


@router.post("/auth/logout")
def logout(request: Request):
    response = JSONResponse({"data": {"logged_out": True}, "error": None})
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(STATE_COOKIE, path="/")
    return response


def _read_state_cookie(request: Request) -> dict | None:
    """Return the signed state payload, or None when absent/tampered/expired.

    Older cookies stored the bare state string; accept both shapes so a sign-in
    already in flight when this deployed does not hard-fail.
    """
    signed = request.cookies.get(STATE_COOKIE)
    if not signed:
        return None
    try:
        payload = _serializer(STATE_SALT).loads(signed, max_age=STATE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if isinstance(payload, str):
        return {"state": payload, "code_verifier": ""}
    return payload if isinstance(payload, dict) else None


def _state_is_valid(request: Request, state: str | None) -> bool:
    payload = _read_state_cookie(request)
    if not payload or not state:
        return False
    return secrets.compare_digest(str(payload.get("state", "")), state)
