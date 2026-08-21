"""Google OAuth web flow for Gmail.

Exactly four scopes, `access_type=offline` + `prompt=consent` so a refresh token
is always returned. Nothing here ever logs a token.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from channels.base import ReauthRequired
from observability.logging import get_logger

_log = get_logger("gmail.oauth")

# ONE consent flow grants both sign-in and Gmail access (spec/api.md).
# Exactly these scopes — `gmail.modify` covers every read + reversible label
# mutation this system performs; nothing broader is ever requested.
GOOGLE_SCOPES: list[str] = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.modify",
]

AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"
USERINFO_URI = "https://www.googleapis.com/oauth2/v2/userinfo"
DEFAULT_REDIRECT_URI = "http://localhost:8001/auth/google/callback"


class OAuthConfigError(RuntimeError):
    """Google OAuth is not configured — actionable, never contains a secret."""


class OAuthExchangeError(RuntimeError):
    """The authorization-code exchange with Google failed."""


@dataclass(frozen=True)
class GoogleOAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str = DEFAULT_REDIRECT_URI
    scopes: list[str] = field(default_factory=lambda: list(GOOGLE_SCOPES))

    def client_config(self) -> dict:
        return {
            "web": {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "auth_uri": AUTH_URI,
                "token_uri": TOKEN_URI,
                "redirect_uris": [self.redirect_uri],
            }
        }


@dataclass(frozen=True)
class OAuthResult:
    """What a completed consent gives us. Never persisted verbatim."""

    refresh_token: str
    access_token: str
    scopes: list[str]
    account_email: str
    display_name: str = ""
    picture_url: str = ""


def _setting(name: str, env: str, default: str = "") -> str:
    """Prefer `config.settings`, fall back to the environment.

    `src/config/settings.py` is owned by another slice; the env fallback keeps
    this module correct regardless of which fields it currently declares.
    """
    try:
        from config.settings import get_settings

        value = getattr(get_settings(), name, "") or ""
    except Exception:  # pragma: no cover - settings unavailable
        value = ""
    return value or os.environ.get(env, "") or default


def google_oauth_config(scopes: Sequence[str] | None = None) -> GoogleOAuthConfig:
    """Build the OAuth config from ``AGENT_GOOGLE_CLIENT_ID/SECRET/REDIRECT_URI``.

    ``scopes`` defaults to :data:`GOOGLE_SCOPES` — the single sign-in + gmail.modify
    consent. There is no separate sign-in-only flow.
    """
    client_id = _setting("google_client_id", "AGENT_GOOGLE_CLIENT_ID")
    client_secret = _setting("google_client_secret", "AGENT_GOOGLE_CLIENT_SECRET")
    redirect_uri = _setting(
        "google_redirect_uri", "AGENT_GOOGLE_REDIRECT_URI", DEFAULT_REDIRECT_URI
    )
    if not client_id:
        raise OAuthConfigError("AGENT_GOOGLE_CLIENT_ID is not set — add it to .env")
    if not client_secret:
        raise OAuthConfigError("AGENT_GOOGLE_CLIENT_SECRET is not set — add it to .env")
    return GoogleOAuthConfig(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scopes=list(scopes) if scopes else list(GOOGLE_SCOPES),
    )


def _flow(config: GoogleOAuthConfig) -> Flow:
    # Google always grants `openid`, `email` and `profile` alongside the Gmail
    # scopes we ask for, so the granted set never equals the requested set and
    # oauthlib's strict equality check aborts the exchange. Relaxing it is the
    # documented way to accept a superset; we still verify the Gmail scopes we
    # depend on below.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    return Flow.from_client_config(
        config.client_config(), scopes=config.scopes, redirect_uri=config.redirect_uri
    )


def build_authorization_url(
    config: GoogleOAuthConfig, *, state: str, scopes: Sequence[str] | None = None
) -> tuple[str, str]:
    """Return `(consent_url, code_verifier)`.

    The Flow generates a PKCE verifier and sends only its challenge to Google.
    The callback builds a *different* Flow object, so the verifier has to travel
    with the request or Google rejects the exchange with "Missing code verifier".
    """
    if scopes is not None:
        config = replace(config, scopes=list(scopes))
    flow = _flow(config)
    url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
        state=state,
    )
    return url, flow.code_verifier or ""


def exchange_code(
    config: GoogleOAuthConfig,
    code: str,
    state: str | None = None,
    code_verifier: str | None = None,
) -> OAuthResult:
    """Trade the authorization code for tokens and confirm the mailbox address."""
    flow = _flow(config)
    if code_verifier:
        flow.code_verifier = code_verifier
    try:
        flow.fetch_token(code=code)
    except Exception as exc:  # oauthlib raises a wide family of errors
        # The user only ever sees "please retry", so the cause has to reach the
        # log or the next failure is undiagnosable. `repr(exc)` carries the
        # oauthlib error class and description, never the code or a token.
        _log.warning("gmail_oauth_exchange_failed", cause=repr(exc))
        raise OAuthExchangeError("Google rejected the authorization code") from exc

    credentials = flow.credentials
    granted = list(credentials.scopes or config.scopes)

    # The mailbox address is the identity anchor: it is what the Gmail client
    # will operate on, so it must come from the Gmail profile, not userinfo.
    profile = _fetch_profile(credentials) if _has_gmail_scope(granted) else {}
    email = profile.get("emailAddress", "")

    # `openid email profile` was granted in the same consent — userinfo gives a
    # real display name and picture (best-effort: sign-in must not fail on it).
    try:
        info = _fetch_userinfo(credentials)
    except OAuthExchangeError:
        info = {}
    email = email or info.get("email", "")
    display_name = info.get("name") or email.split("@")[0]

    return OAuthResult(
        refresh_token=credentials.refresh_token or "",
        access_token=credentials.token or "",
        scopes=granted,
        account_email=email,
        display_name=display_name,
        picture_url=info.get("picture", "") or "",
    )


def _has_gmail_scope(scopes: Sequence[str]) -> bool:
    return any("gmail." in scope for scope in scopes)


def _fetch_profile(credentials: Credentials) -> dict:
    from googleapiclient.discovery import build

    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    try:
        return service.users().getProfile(userId="me").execute()
    except Exception as exc:
        raise OAuthExchangeError("could not read the Gmail profile after consent") from exc


def _fetch_userinfo(credentials: Credentials) -> dict:
    """Read `{email, name}` from Google's userinfo endpoint (sign-in intent)."""
    from google.auth.transport.requests import AuthorizedSession

    try:
        response = AuthorizedSession(credentials).get(USERINFO_URI, timeout=15)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise OAuthExchangeError("could not read your Google profile after consent") from exc
    return payload if isinstance(payload, dict) else {}


def revoke_refresh_token(refresh_token: str) -> bool:
    """Best-effort revocation at Google. Returns True on success, never raises.

    A Google outage must never leave a stored token we cannot remove, so the
    caller deletes the local ciphertext regardless of what this returns.
    """
    if not refresh_token:
        return False
    try:
        import requests

        response = requests.post(
            REVOKE_URI,
            data={"token": refresh_token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        return response.status_code == 200
    except Exception as exc:  # noqa: BLE001 — best effort by contract
        _log.warning("google_token_revoke_failed", cause=repr(exc))
        return False


def credentials_from_refresh_token(
    config: GoogleOAuthConfig, refresh_token: str
) -> Credentials:
    """Build auto-refreshing credentials from a stored refresh token."""
    if not refresh_token:
        raise ReauthRequired("no stored refresh token — reconnect Gmail")
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=TOKEN_URI,
        client_id=config.client_id,
        client_secret=config.client_secret,
        scopes=list(config.scopes),
    )


def get_credentials(user_id: str) -> Credentials:
    """Load the user's stored refresh token and mint auto-refreshing credentials.

    THE credentials-loading seam for the read layer
    (``channels.gmail.client.list_inbox_threads``): a revoked/absent token
    surfaces here — tests simulate invalid_grant by patching this function
    (tests/fixtures/fake_gmail.py ``force_refresh_failure``).
    """
    from channels.gmail.store import SqlConnectionStore

    try:
        refresh_token = SqlConnectionStore().load_refresh_token(user_id=user_id)
    except ReauthRequired:
        raise
    except Exception as exc:  # noqa: BLE001 — undecryptable token == reconnect
        raise ReauthRequired(
            "the stored Gmail token could not be read — reconnect Gmail"
        ) from exc
    return credentials_from_refresh_token(google_oauth_config(), refresh_token)


def refresh_access_token(credentials: Credentials) -> Credentials:
    """Force a refresh now; surface an invalid grant as `ReauthRequired`."""
    try:
        credentials.refresh(GoogleAuthRequest())
    except Exception as exc:
        raise ReauthRequired("the stored Gmail refresh token is no longer valid") from exc
    return credentials
