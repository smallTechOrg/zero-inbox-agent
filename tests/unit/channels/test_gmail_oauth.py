"""Unit tests for the Google OAuth configuration and URL construction."""

from urllib.parse import parse_qs, urlparse

import pytest

REQUIRED_SCOPES = {
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/gmail.compose",
}


@pytest.fixture
def oauth_config(monkeypatch):
    from channels.gmail import oauth

    monkeypatch.setenv("AGENT_GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("AGENT_GOOGLE_CLIENT_SECRET", "csecret")
    monkeypatch.setenv(
        "AGENT_GOOGLE_REDIRECT_URI", "http://localhost:8001/auth/google/callback"
    )
    return oauth.google_oauth_config()


def test_exactly_the_four_gmail_scopes_are_requested_and_no_more():
    from channels.gmail.oauth import GOOGLE_SCOPES

    assert set(GOOGLE_SCOPES) == REQUIRED_SCOPES


def test_no_destructive_or_broad_scope_is_ever_requested():
    from channels.gmail.oauth import GOOGLE_SCOPES

    joined = " ".join(GOOGLE_SCOPES)
    assert "https://mail.google.com/" not in joined
    assert "drive" not in joined and "contacts" not in joined and "calendar" not in joined


def test_authorization_url_carries_offline_access_forced_consent_and_the_state(oauth_config):
    from channels.gmail.oauth import build_authorization_url

    url, verifier = build_authorization_url(oauth_config, state="csrf-123")
    query = parse_qs(urlparse(url).query)

    assert urlparse(url).netloc == "accounts.google.com"
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["state"] == ["csrf-123"]
    assert query["redirect_uri"] == ["http://localhost:8001/auth/google/callback"]
    assert set(query["scope"][0].split()) >= REQUIRED_SCOPES


def test_the_client_secret_never_appears_in_the_authorization_url(oauth_config):
    from channels.gmail.oauth import build_authorization_url

    url, _ = build_authorization_url(oauth_config, state="csrf-123")

    assert "csecret" not in url


def test_missing_google_client_id_raises_a_clear_configuration_error(monkeypatch):
    from channels.gmail.oauth import OAuthConfigError, google_oauth_config

    monkeypatch.setenv("AGENT_GOOGLE_CLIENT_ID", "")
    monkeypatch.setenv("AGENT_GOOGLE_CLIENT_SECRET", "csecret")

    with pytest.raises(OAuthConfigError) as excinfo:
        google_oauth_config()

    assert "AGENT_GOOGLE_CLIENT_ID" in str(excinfo.value)


def test_missing_google_client_secret_raises_a_clear_configuration_error(monkeypatch):
    from channels.gmail.oauth import OAuthConfigError, google_oauth_config

    monkeypatch.setenv("AGENT_GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("AGENT_GOOGLE_CLIENT_SECRET", "")

    with pytest.raises(OAuthConfigError):
        google_oauth_config()


def test_redirect_uri_defaults_to_the_documented_localhost_callback(monkeypatch):
    from channels.gmail.oauth import google_oauth_config

    monkeypatch.setenv("AGENT_GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("AGENT_GOOGLE_CLIENT_SECRET", "csecret")
    monkeypatch.delenv("AGENT_GOOGLE_REDIRECT_URI", raising=False)

    assert (
        google_oauth_config().redirect_uri
        == "http://localhost:8001/auth/google/callback"
    )


def test_credentials_from_refresh_token_are_built_with_refresh_enabled(oauth_config):
    from channels.gmail.oauth import credentials_from_refresh_token

    creds = credentials_from_refresh_token(oauth_config, "1//refresh")

    assert creds.refresh_token == "1//refresh"
    assert creds.token_uri.endswith("/token")
    assert set(creds.scopes) == REQUIRED_SCOPES
    assert creds.valid is False  # no access token yet -> will refresh on first use


def test_an_empty_refresh_token_is_rejected(oauth_config):
    from channels.base import ReauthRequired
    from channels.gmail.oauth import credentials_from_refresh_token

    with pytest.raises(ReauthRequired):
        credentials_from_refresh_token(oauth_config, "")
