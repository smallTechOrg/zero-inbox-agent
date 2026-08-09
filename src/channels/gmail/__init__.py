"""Gmail — the first (and, in v1, only) `ChannelAdapter` implementation."""

from channels.gmail.adapter import GmailAdapter
from channels.gmail.oauth import (
    GOOGLE_SCOPES,
    GoogleOAuthConfig,
    OAuthConfigError,
    OAuthExchangeError,
    OAuthResult,
    build_authorization_url,
    credentials_from_refresh_token,
    exchange_code,
    google_oauth_config,
)

__all__ = [
    "GmailAdapter",
    "GOOGLE_SCOPES",
    "GoogleOAuthConfig",
    "OAuthConfigError",
    "OAuthExchangeError",
    "OAuthResult",
    "build_authorization_url",
    "credentials_from_refresh_token",
    "exchange_code",
    "google_oauth_config",
]
