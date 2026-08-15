"""Google OAuth web flow + dashboard session.

Phase 8 splits one flow into two **intents**:

* ``intent=signin`` asks Google for ``openid email profile`` only. It upserts the
  ``users`` row, issues a session and returns. It writes **no** ``channel_accounts``
  row, requires **no** refresh token and starts **no** triage run.
* ``intent=connect`` (and a missing ``intent``) is the pre-Phase-8 behaviour, byte
  for byte, including the auto-triage background task.

The intent round-trips inside the **signed** ``zi_oauth_state`` cookie
(``{state, code_verifier, intent}``) and is read only from there on the callback —
read from the query string it would be attacker-controlled, and the scope decision
along with it.

Routes
------
GET  /auth/google/start     302 → Google consent (scopes chosen by intent)
GET  /auth/google/callback  code → tokens → user (+ connection) + session → 302 /app/
POST /auth/logout           revokes the current user_sessions row, clears the cookie

Cookie contract (owned by `api/session.py`): ``zi_session`` is an itsdangerous
``URLSafeTimedSerializer`` token, salt ``zi-session-v1``, payload ``{"uid", "sid"}``,
signed with ``AGENT_SECRET_KEY`` — which is **required**; there is no dev fallback.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from api.session import (
    COOKIE_NAME,
    create_user_session,
    issue_session_token,
    read_session_payload,
    read_session_token,
    revoke_session,
    set_session_cookie,
)
from channels.base import ReauthRequired
from channels.gmail.oauth import (
    CONNECT_SCOPES,
    SIGNIN_SCOPES,
    OAuthConfigError,
    OAuthExchangeError,
    OAuthResult,
    build_authorization_url,
    exchange_code,
    google_oauth_config,
)
from channels.gmail.store import MailboxOwnedByAnotherUser, SqlConnectionStore
from security.crypto import TokenCipher, get_secret_key

__all__ = [
    "router",
    "get_connection_store",
    "get_code_exchanger",
    "issue_session_cookie",
    "read_session_user_id",
    "OAuthExchangeError",
    "SESSION_COOKIE",
    "INTENT_SIGNIN",
    "INTENT_CONNECT",
]

router = APIRouter(tags=["auth"])

SESSION_COOKIE = "zi_session"
STATE_COOKIE = "zi_oauth_state"
SESSION_SALT = "zi-session"
STATE_SALT = "zi-oauth-state"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
STATE_MAX_AGE = 60 * 10
DASHBOARD_URL = "/app/"

INTENT_SIGNIN = "signin"
INTENT_CONNECT = "connect"
#: A missing or unrecognised intent means `connect` — the pre-Phase-8 behaviour.
VALID_INTENTS = (INTENT_SIGNIN, INTENT_CONNECT)


# --- session cookie ----------------------------------------------------


class SecretKeyMissing(RuntimeError):
    """``AGENT_SECRET_KEY`` is unset. Phase 8 removed the dev fallback.

    Signing session cookies with a public constant meant anyone could mint a
    cookie for any user id, so a missing key now fails loudly at startup rather
    than quietly producing forgeable sessions.
    """


def _serializer(salt: str) -> URLSafeTimedSerializer:
    key = get_secret_key()
    if not key:
        raise SecretKeyMissing(
            "AGENT_SECRET_KEY is not set — add it to .env. Sessions are never "
            "signed with a fallback key."
        )
    return URLSafeTimedSerializer(key, salt=salt)


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
    """Returns `(code, state, code_verifier, scopes=?) -> OAuthResult`.

    ``scopes`` is keyword-only and optional so the connect path calls it with
    exactly the arguments it always did.
    """

    def _exchange(
        code: str,
        state: str | None,
        code_verifier: str | None = None,
        *,
        scopes=None,
    ) -> OAuthResult:
        return exchange_code(
            google_oauth_config(scopes), code, state, code_verifier
        )

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
def google_start(request: Request, intent: str | None = None):
    """Send the user to the real Google consent screen with a CSRF state.

    ``intent=signin`` requests name and email only; anything else (including no
    intent at all) requests the Gmail connect scopes exactly as before.
    """
    resolved_intent = intent if intent in VALID_INTENTS else INTENT_CONNECT
    scopes = SIGNIN_SCOPES if resolved_intent == INTENT_SIGNIN else CONNECT_SCOPES

    try:
        config = google_oauth_config(scopes)
    except OAuthConfigError as exc:
        return _error("validation_error", str(exc), 422)

    state = secrets.token_urlsafe(24)
    url, code_verifier = build_authorization_url(config, state=state, scopes=scopes)

    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        STATE_COOKIE,
        # The PKCE verifier rides along in the same signed, httponly cookie as
        # the CSRF state — it must reach the callback to complete the exchange.
        # So does the intent: read from the query string on the callback it would
        # be attacker-controlled, and with it the scope decision.
        _serializer(STATE_SALT).dumps(
            {
                "state": state,
                "code_verifier": code_verifier,
                "intent": resolved_intent,
            }
        ),
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

    state_payload = _read_state_cookie(request) or {}
    verifier = state_payload.get("code_verifier") or None
    # The intent comes from the SIGNED cookie only — never from the query string.
    intent = state_payload.get("intent")
    intent = intent if intent in VALID_INTENTS else INTENT_CONNECT

    try:
        if intent == INTENT_SIGNIN:
            result: OAuthResult = exchanger(code, state, verifier, scopes=SIGNIN_SCOPES)
        else:
            # Byte for byte the pre-Phase-8 call.
            result = exchanger(code, state, verifier)
    except ReauthRequired as exc:
        return _error("reauth_required", str(exc), 409)
    except OAuthExchangeError:
        return _error(
            "provider_error", "Google could not complete the sign-in. Please retry.", 502
        )

    if intent == INTENT_SIGNIN:
        return _complete_signin(request, result, store)

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
    try:
        user_id, connection_id = store.upsert_user_and_connection(
            email=result.account_email,
            display_name=result.display_name or result.account_email.split("@")[0],
            refresh_token_enc=refresh_token_enc,
            scopes=list(result.scopes),
            channel="gmail",
        )
    except MailboxOwnedByAnotherUser as exc:
        # Nothing was written — the guard runs before the first insert. Two
        # agents must never mutate one inbox under two independent policies.
        return _error("mailbox_already_connected", str(exc), 409)

    background_tasks.add_task(_auto_triage_task, user_id=user_id, connection_id=connection_id)

    response = RedirectResponse(DASHBOARD_URL, status_code=302)
    _issue_session(response, request, user_id)
    response.delete_cookie(STATE_COOKIE, path="/")
    return response


def _complete_signin(request: Request, result: OAuthResult, store: SqlConnectionStore):
    """`intent=signin`: identity + session, and nothing else.

    No ``channel_accounts`` row, no refresh-token requirement, no triage task.
    """
    if not result.account_email:
        return _error("provider_error", "Google did not return an email address.", 502)

    user_id = store.upsert_user(
        email=result.account_email,
        display_name=result.display_name or result.account_email.split("@")[0],
    )

    response = RedirectResponse(DASHBOARD_URL, status_code=302)
    _issue_session(response, request, user_id)
    response.delete_cookie(STATE_COOKIE, path="/")
    return response


def _issue_session(response, request: Request, user_id: str) -> None:
    """Create a fresh ``user_sessions`` row and rotate the cookie onto it."""
    session_id = create_user_session(user_id, request)
    set_session_cookie(response, user_id, session_id, request=request)


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
    """Revoke the current ``user_sessions`` row, then clear the cookie.

    Clearing the cookie alone was never enough: a copy of it replayed from
    anywhere still authenticated. Signing out now ends the session server-side.
    """
    token = request.cookies.get(COOKIE_NAME)
    payload = read_session_payload(token) if token else None
    if payload and payload.get("sid"):
        try:
            revoke_session(payload["sid"], payload["uid"])
        except Exception:  # noqa: BLE001 — sign-out must always clear the cookie
            pass

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
