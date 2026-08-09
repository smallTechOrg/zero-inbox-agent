"""Google OAuth web flow for Gmail.

Exactly four scopes, `access_type=offline` + `prompt=consent` so a refresh token
is always returned. Nothing here ever logs a token.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from channels.base import ReauthRequired

GOOGLE_SCOPES: list[str] = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/gmail.compose",
]

AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
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


def google_oauth_config() -> GoogleOAuthConfig:
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
        client_id=client_id, client_secret=client_secret, redirect_uri=redirect_uri
    )


def _flow(config: GoogleOAuthConfig) -> Flow:
    return Flow.from_client_config(
        config.client_config(), scopes=config.scopes, redirect_uri=config.redirect_uri
    )


def build_authorization_url(config: GoogleOAuthConfig, *, state: str) -> str:
    url, _ = _flow(config).authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
        state=state,
    )
    return url


def exchange_code(config: GoogleOAuthConfig, code: str, state: str | None = None) -> OAuthResult:
    """Trade the authorization code for tokens and confirm the mailbox address."""
    flow = _flow(config)
    try:
        flow.fetch_token(code=code)
    except Exception as exc:  # oauthlib raises a wide family of errors
        raise OAuthExchangeError("Google rejected the authorization code") from exc

    credentials = flow.credentials
    profile = _fetch_profile(credentials)
    return OAuthResult(
        refresh_token=credentials.refresh_token or "",
        access_token=credentials.token or "",
        scopes=list(credentials.scopes or config.scopes),
        account_email=profile.get("emailAddress", ""),
        display_name=profile.get("emailAddress", "").split("@")[0],
    )


def _fetch_profile(credentials: Credentials) -> dict:
    from googleapiclient.discovery import build

    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    try:
        return service.users().getProfile(userId="me").execute()
    except Exception as exc:
        raise OAuthExchangeError("could not read the Gmail profile after consent") from exc


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


def refresh_access_token(credentials: Credentials) -> Credentials:
    """Force a refresh now; surface an invalid grant as `ReauthRequired`."""
    try:
        credentials.refresh(GoogleAuthRequest())
    except Exception as exc:
        raise ReauthRequired("the stored Gmail refresh token is no longer valid") from exc
    return credentials
