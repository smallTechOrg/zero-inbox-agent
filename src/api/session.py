"""Signed session cookie + the current-user surface.

The session cookie (``zi_session``) carries ``{uid, sid}``, signed with
``AGENT_SECRET_KEY`` via itsdangerous. Every ``/api/*`` route depends on
:func:`require_user_id`, so a route can only ever see rows for the signed-in user
— it is the **single** user-scope chokepoint and Phase 8 adds revocation *inside*
it rather than a second auth path.

``api/auth.py`` (Google OAuth) calls :func:`set_session_cookie` after it upserts the
user, and :func:`clear_session_cookie` on logout.

Phase 8 session hardening
-------------------------
* ``sid`` names a ``user_sessions`` row. A revoked or unknown ``sid`` is ``401``.
* A **legacy** ``uid``-only cookie (every session issued before Phase 8) stays
  valid: it authenticates, gains a ``user_sessions`` row, and the cookie is
  re-issued with a ``sid``. No user is signed out by this migration.
* ``last_seen_at`` is written at most once per :data:`LAST_SEEN_THROTTLE_SECONDS`
  per session, so the device list is useful without a write per request.
* The cookie is ``secure`` whenever the request scheme is https.
* **No raw IP and no raw user-agent string is stored, logged or returned.**
"""

from __future__ import annotations

import hashlib
import hmac
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from api._common import UNAUTHENTICATED, VALIDATION_ERROR, api_error, iso, ok
from db.session import get_session
from graph.autonomy import DEFAULT_AUTO_ACT_THRESHOLD, MODEL_CEILING_WARNING_THRESHOLD
from tools.never_miss import DEFAULT_CONFIDENCE_FLOOR

COOKIE_NAME = "zi_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
_SALT = "zi-session-v1"

#: spec/api.md: `last_seen_at` is updated at most once per 60 s per session.
LAST_SEEN_THROTTLE_SECONDS = 60

#: Legacy `uid`-only cookie -> the `user_sessions` row we minted for it. Without
#: this, a legacy cookie replayed on a route that returns a bare `Response`
#: (which cannot carry the re-issued cookie) would mint a fresh row per request.
#: Always re-validated against the DB before use, so it can never authenticate a
#: row that has been revoked or belongs to another database.
#:
#: It is **bounded**: process-global state that grows with every distinct legacy
#: cookie ever seen is a leak, and it outlives a test's database. Eviction is
#: never a correctness event — an evicted cookie simply mints a fresh session row
#: on its next request, exactly as it did before it was ever cached.
_LEGACY_UPGRADE_CACHE_SIZE = 512
_LEGACY_UPGRADES: OrderedDict[str, str] = OrderedDict()


def _remember_legacy_upgrade(token: str, sid: str) -> None:
    """Record `token -> sid`, evicting the least-recently-used entry past the cap."""
    _LEGACY_UPGRADES.pop(token, None)
    _LEGACY_UPGRADES[token] = sid
    while len(_LEGACY_UPGRADES) > _LEGACY_UPGRADE_CACHE_SIZE:
        _LEGACY_UPGRADES.popitem(last=False)

router = APIRouter()


def _secret_key() -> str:
    """Return the signing key, or raise the one canonical fatal-config error.

    This used to raise a bespoke 500 whose text differed from the startup error for
    the very same cause, so the same misconfiguration read as two unrelated bugs.
    ``require_secret_key()`` is the single source of both the message and the
    ``exit_code`` the supervisor keys off (see ``src/config/settings.py``).
    """
    from config.settings import require_secret_key

    return require_secret_key()


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_secret_key(), salt=_SALT)


def issue_session_token(user_id: str, session_id: str | None = None) -> str:
    """Mint the signed cookie value. ``session_id`` is the ``user_sessions`` row id.

    Omitting it produces the legacy ``uid``-only shape, which still authenticates
    (and is upgraded in place on first use).
    """
    payload: dict = {"uid": user_id}
    if session_id:
        payload["sid"] = session_id
    return _serializer().dumps(payload)


def read_session_payload(token: str) -> dict | None:
    """Return the verified cookie payload ``{uid, sid?}``, or None."""
    try:
        payload = _serializer().loads(token, max_age=COOKIE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(payload, dict):
        return None
    uid = payload.get("uid")
    if not isinstance(uid, str) or not uid:
        return None
    sid = payload.get("sid")
    return {"uid": uid, "sid": sid if isinstance(sid, str) and sid else None}


def read_session_token(token: str) -> str | None:
    """Signature-only read of the user id. Does **not** check revocation."""
    payload = read_session_payload(token)
    return payload["uid"] if payload else None


def _is_https(request: Request | None) -> bool:
    """True when this request arrived over TLS.

    Reads ``request.url.scheme``, falling back to the raw ASGI scope: building a
    ``URL`` from a scope with no server and no Host header drops the scheme, and
    silently returning False there would mean shipping a non-``secure`` session
    cookie over https.
    """
    if request is None:
        return False
    try:
        if request.url.scheme == "https":
            return True
    except Exception:  # noqa: BLE001 — fall through to the scope
        pass
    return request.scope.get("scheme") == "https"


def set_session_cookie(
    response: Response,
    user_id: str,
    session_id: str | None = None,
    *,
    request: Request | None = None,
) -> None:
    response.set_cookie(
        COOKIE_NAME,
        issue_session_token(user_id, session_id),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        # Phase 8: `secure` follows the scheme. Hardcoding False would have sent
        # the session cookie in clear over any https deployment.
        secure=_is_https(request),
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


# --- user_sessions -----------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def summarize_user_agent(raw: str | None) -> str:
    """Derive "Chrome on macOS" from a UA string. The raw string is never stored."""
    ua = raw or ""
    lowered = ua.lower()
    browser = next(
        (
            name
            for token, name in (
                ("edg/", "Edge"),
                ("opr/", "Opera"),
                ("chrome/", "Chrome"),
                ("firefox/", "Firefox"),
                ("safari/", "Safari"),
            )
            if token in lowered
        ),
        "",
    )
    platform = next(
        (
            name
            for token, name in (
                ("iphone", "iPhone"),
                ("ipad", "iPad"),
                ("android", "Android"),
                ("mac os x", "macOS"),
                ("macintosh", "macOS"),
                ("windows", "Windows"),
                ("cros", "ChromeOS"),
                ("linux", "Linux"),
            )
            if token in lowered
        ),
        "",
    )
    if browser and platform:
        return f"{browser} on {platform}"
    return browser or platform or "Unknown device"


def hash_client_ip(request: Request | None) -> str | None:
    """HMAC-SHA256 of the client IP. The raw IP never leaves this function."""
    if request is None or request.client is None or not request.client.host:
        return None
    try:
        key = _secret_key().encode()
    except Exception:  # noqa: BLE001 — telemetry must never fail a request
        return None
    return hmac.new(key, request.client.host.encode(), hashlib.sha256).hexdigest()


def create_user_session(user_id: str, request: Request | None = None) -> str:
    """Create a ``user_sessions`` row and return its id (the cookie's ``sid``)."""
    from db.models import UserSession
    from db.session import create_db_session

    session_id = str(uuid4())
    now = _now()
    with create_db_session() as db:
        db.add(
            UserSession(
                id=session_id,
                user_id=user_id,
                created_at=now,
                last_seen_at=now,
                user_agent_summary=summarize_user_agent(
                    request.headers.get("user-agent") if request else None
                ),
                ip_hash=hash_client_ip(request),
            )
        )
    return session_id


def revoke_session(session_id: str, user_id: str | None = None) -> bool:
    """Set ``revoked_at``. Returns False if there is no such (user-scoped) row."""
    from db.models import UserSession
    from db.session import create_db_session

    with create_db_session() as db:
        row = db.get(UserSession, session_id)
        if row is None or (user_id is not None and row.user_id != user_id):
            return False
        if row.revoked_at is None:
            row.revoked_at = _now()
        return True


def revoke_all_sessions(user_id: str) -> int:
    """Revoke every live session for the user, including the current one."""
    from db.models import UserSession
    from db.session import create_db_session

    now = _now()
    with create_db_session() as db:
        rows = (
            db.query(UserSession)
            .filter(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
            .all()
        )
        for row in rows:
            row.revoked_at = now
        return len(rows)


def _resolve_session(sid: str, uid: str) -> bool:
    """True when ``sid`` is a live session for ``uid``; touches ``last_seen_at``."""
    from db.models import UserSession
    from db.session import create_db_session

    with create_db_session() as db:
        row = db.get(UserSession, sid)
        if row is None or row.user_id != uid or row.revoked_at is not None:
            return False
        now = _now()
        last_seen = row.last_seen_at
        if last_seen is not None and last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        if last_seen is None or now - last_seen >= timedelta(
            seconds=LAST_SEEN_THROTTLE_SECONDS
        ):
            row.last_seen_at = now
        return True


def optional_user_id(request: Request) -> str | None:
    """Signature-only read, for logging context. Not an authorization decision."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return read_session_token(token)


def current_session_id(request: Request) -> str | None:
    """The ``sid`` on the incoming cookie, if any — used to mark "This device"."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    payload = read_session_payload(token)
    return payload["sid"] if payload else None


def require_user_id(request: Request, response: Response) -> str:
    """FastAPI dependency — the user-scope guard for every /api/* route.

    Phase 8 adds revocation here, inside the existing chokepoint: a revoked or
    unknown ``sid`` is ``401``. A legacy ``uid``-only cookie authenticates and is
    upgraded in place, so this phase signs nobody out.
    """
    token = request.cookies.get(COOKIE_NAME)
    payload = read_session_payload(token) if token else None
    if payload is None:
        raise api_error(UNAUTHENTICATED, "Sign in with Google to continue.")

    uid = payload["uid"]
    sid = payload["sid"]

    if sid:
        if not _resolve_session(sid, uid):
            raise api_error(UNAUTHENTICATED, "This session was signed out. Sign in again.")
        return uid

    # Legacy `uid`-only cookie: authenticate, then upgrade in place.
    cached = _LEGACY_UPGRADES.get(token or "")
    if cached and _resolve_session(cached, uid):
        sid = cached
        if token:
            _LEGACY_UPGRADES.move_to_end(token)
    else:
        _LEGACY_UPGRADES.pop(token or "", None)
        sid = create_user_session(uid, request)
        if token:
            _remember_legacy_upgrade(token, sid)
    if response is not None:
        set_session_cookie(response, uid, sid, request=request)
    return uid


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
    """Settings are returned with the shipped defaults when the row does not exist yet.

    ``auto_act_threshold`` defaults to ``DEFAULT_AUTO_ACT_THRESHOLD`` (0.80, Phase 7)
    rather than the old 0.95: 0.95 sits above the model's entire measured output
    range, so it is a bar the agent can never clear. See spec/data.md.
    """
    if row is None:
        return {
            "auto_act_threshold": DEFAULT_AUTO_ACT_THRESHOLD,
            "confidence_floor": DEFAULT_CONFIDENCE_FLOOR,
            "dry_run": True,
            "llm_model": "",
            "digest_hour_local": 8,
            "timezone": "UTC",
        }
    return {
        "auto_act_threshold": row.auto_act_threshold,
        "confidence_floor": row.confidence_floor,
        "dry_run": row.dry_run,
        "llm_model": row.llm_model,
        "digest_hour_local": row.digest_hour_local,
        "timezone": row.timezone,
    }


def _validate_threshold(field: str, value) -> float:
    """Rule A5: ``0 < threshold <= 1``. Rejected with ``validation_error``."""
    try:
        threshold = float(value)
    except (TypeError, ValueError) as exc:
        raise api_error(VALIDATION_ERROR, f"{field} must be a number between 0 and 1") from exc
    if not (0 < threshold <= 1):
        raise api_error(
            VALIDATION_ERROR, f"{field} must be greater than 0 and at most 1"
        )
    return threshold


@router.patch("/api/settings")
def update_settings(
    body: dict,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    """Partial update of the signed-in user's settings row.

    Only fields present in the request body are changed. Creates the row with
    defaults (overridden by the given fields) if it does not exist yet.
    """
    from db.models import UserSettings

    allowed_fields = {
        "auto_act_threshold",
        "confidence_floor",
        "dry_run",
        "llm_model",
        "timezone",
        "digest_hour_local",
    }
    updates = {k: v for k, v in body.items() if k in allowed_fields}

    # Phase 7 Rule A5. Both bars are validated the same way; the autonomy bar is
    # additionally warned about above the measured model ceiling.
    warning: str | None = None
    for field in ("auto_act_threshold", "confidence_floor"):
        if field in updates:
            updates[field] = _validate_threshold(field, updates[field])
    if updates.get("auto_act_threshold", 0) > MODEL_CEILING_WARNING_THRESHOLD:
        # Accepted, not rejected — it is the user's inbox. But the measured model
        # ceiling is ~0.94, so a bar above 0.90 means the agent acts on almost
        # nothing, and the UI must say so rather than shipping a silent no-op.
        warning = "above_model_ceiling"

    row = session.get(UserSettings, user_id)
    if row is None:
        row = UserSettings(
            user_id=user_id,
            auto_act_threshold=updates.get(
                "auto_act_threshold", DEFAULT_AUTO_ACT_THRESHOLD
            ),
            confidence_floor=updates.get("confidence_floor", DEFAULT_CONFIDENCE_FLOOR),
            dry_run=updates.get("dry_run", True),
            llm_model=updates.get("llm_model", ""),
            digest_hour_local=updates.get("digest_hour_local", 8),
            timezone=updates.get("timezone", "UTC"),
        )
        session.add(row)
    else:
        for key, value in updates.items():
            setattr(row, key, value)

    session.commit()
    session.refresh(row)

    payload = _settings_payload(row)
    if warning:
        payload["warning"] = warning
    return ok(payload)
