"""Gmail — the only channel in v1: OAuth, INBOX-only reads, the four reversible
mutations, and the mini-audit data source."""

from channels.gmail.adapter import GmailAdapter
from channels.gmail.audit import collect_inbox_snapshot
from channels.gmail.mutations import (
    ALLOWED_MUTATIONS,
    INVERSE_MUTATION,
    ForbiddenMutation,
    GmailMutator,
)
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
from channels.gmail.store import SqlConnectionStore, adapter_for_user

__all__ = [
    "GmailAdapter",
    "collect_inbox_snapshot",
    "ALLOWED_MUTATIONS",
    "INVERSE_MUTATION",
    "ForbiddenMutation",
    "GmailMutator",
    "GOOGLE_SCOPES",
    "GoogleOAuthConfig",
    "OAuthConfigError",
    "OAuthExchangeError",
    "OAuthResult",
    "build_authorization_url",
    "credentials_from_refresh_token",
    "exchange_code",
    "google_oauth_config",
    "SqlConnectionStore",
    "adapter_for_user",
]
