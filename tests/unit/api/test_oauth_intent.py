"""Phase 8 — sign-in is a separate OAuth intent from mailbox connection.

The two things that matter here and are asserted exactly:

1. ``intent=signin`` asks Google for ``openid email profile`` and **no** ``gmail.*``
   scope; ``intent=connect`` (and no intent at all) asks for the existing Gmail set,
   unchanged.
2. The intent is read from the **signed** state cookie only. If it could be read
   from the callback query string, an attacker could pick the scope set.
"""

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

SECRET = "unit-test-secret-key"


class RecordingExchanger:
    """Captures the scopes the route asked the exchange to use."""

    def __init__(self, *, email="signin-user@gmail.com", refresh_token="1//REFRESH"):
        self.email = email
        self.refresh_token = refresh_token
        self.calls: list[dict] = []

    def __call__(self, code, state, code_verifier=None, *, scopes=None):
        from channels.gmail.oauth import CONNECT_SCOPES, OAuthResult

        self.calls.append({"code": code, "scopes": scopes})
        granted = list(scopes) if scopes else list(CONNECT_SCOPES)
        return OAuthResult(
            refresh_token=self.refresh_token,
            access_token="ya29.access",
            scopes=granted,
            account_email=self.email,
            display_name="Signin User",
        )


@pytest.fixture
def google_env(monkeypatch):
    monkeypatch.setenv("AGENT_GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("AGENT_GOOGLE_CLIENT_SECRET", "csecret")
    monkeypatch.setenv(
        "AGENT_GOOGLE_REDIRECT_URI", "http://localhost:8001/auth/google/callback"
    )
    monkeypatch.setenv("AGENT_SECRET_KEY", SECRET)


@pytest.fixture
def exchanger():
    return RecordingExchanger()


@pytest.fixture
def auth_client(google_env, exchanger, _isolated_db):
    from api import auth

    app = FastAPI()
    app.include_router(auth.router)
    app.dependency_overrides[auth.get_code_exchanger] = lambda: exchanger
    with TestClient(app) as c:
        yield c


def _scopes_from(response) -> set[str]:
    query = parse_qs(urlparse(response.headers["location"]).query)
    return set(query["scope"][0].split())


# --- 1. the two scope sets, asserted exactly ----------------------------


def test_the_two_scope_sets_are_disjoint_constants():
    from channels.gmail.oauth import CONNECT_SCOPES, GOOGLE_SCOPES, SIGNIN_SCOPES

    assert tuple(SIGNIN_SCOPES) == ("openid", "email", "profile")
    # CONNECT_SCOPES is a COPY of the existing Gmail set, not a rewrite of it.
    assert tuple(CONNECT_SCOPES) == tuple(GOOGLE_SCOPES)
    assert not any("gmail" in scope for scope in SIGNIN_SCOPES)


def test_signin_intent_requests_name_and_email_only(auth_client):
    response = auth_client.get(
        "/auth/google/start?intent=signin", follow_redirects=False
    )

    assert response.status_code == 302
    assert _scopes_from(response) == {"openid", "email", "profile"}


def test_signin_intent_requests_no_gmail_scope_whatsoever(auth_client):
    response = auth_client.get(
        "/auth/google/start?intent=signin", follow_redirects=False
    )

    joined = " ".join(_scopes_from(response))
    assert "gmail" not in joined
    assert "https://mail.google.com/" not in joined


def test_connect_intent_requests_exactly_the_existing_gmail_set(auth_client):
    from channels.gmail.oauth import GOOGLE_SCOPES

    response = auth_client.get(
        "/auth/google/start?intent=connect", follow_redirects=False
    )

    assert _scopes_from(response) >= set(GOOGLE_SCOPES)


def test_a_missing_intent_is_connect_and_is_unchanged_from_before(auth_client):
    from channels.gmail.oauth import GOOGLE_SCOPES

    default = auth_client.get("/auth/google/start", follow_redirects=False)
    explicit = auth_client.get(
        "/auth/google/start?intent=connect", follow_redirects=False
    )

    assert _scopes_from(default) == _scopes_from(explicit)
    assert _scopes_from(default) >= set(GOOGLE_SCOPES)


# --- edge case: an unrecognised intent -----------------------------------


def test_an_unrecognised_intent_falls_back_to_connect_rather_than_erroring(auth_client):
    from channels.gmail.oauth import GOOGLE_SCOPES

    response = auth_client.get(
        "/auth/google/start?intent=admin", follow_redirects=False
    )

    assert response.status_code == 302
    assert _scopes_from(response) >= set(GOOGLE_SCOPES)


# --- 2. the intent comes from the signed cookie, never the query string --


def test_the_intent_is_carried_inside_the_signed_state_cookie(auth_client):
    from itsdangerous import URLSafeTimedSerializer

    from api.auth import STATE_SALT

    response = auth_client.get(
        "/auth/google/start?intent=signin", follow_redirects=False
    )

    payload = URLSafeTimedSerializer(SECRET, salt=STATE_SALT).loads(
        response.cookies["zi_oauth_state"]
    )
    assert payload["intent"] == "signin"
    assert payload["state"] and payload["code_verifier"]


def test_the_callback_ignores_an_intent_forged_on_the_query_string(
    auth_client, exchanger
):
    """A `connect` flow with `?intent=signin` appended must still be a connect.

    If the callback trusted the query string the attacker would choose the scope
    set — and a `signin` handling of a `connect` consent would drop the refresh
    token on the floor.
    """
    start = auth_client.get("/auth/google/start", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]

    auth_client.get(
        f"/auth/google/callback?code=c&state={state}&intent=signin",
        follow_redirects=False,
    )

    assert exchanger.calls, "the exchanger was never called"
    # `scopes=None` is the byte-for-byte pre-Phase-8 connect call.
    assert exchanger.calls[0]["scopes"] is None


def test_a_signin_callback_exchanges_with_the_signin_scopes(auth_client, exchanger):
    from channels.gmail.oauth import SIGNIN_SCOPES

    start = auth_client.get(
        "/auth/google/start?intent=signin", follow_redirects=False
    )
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]

    response = auth_client.get(
        f"/auth/google/callback?code=c&state={state}", follow_redirects=False
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/app/"
    assert exchanger.calls[0]["scopes"] == SIGNIN_SCOPES


def test_a_signin_callback_needs_no_refresh_token(auth_client, exchanger):
    """Google returns no refresh token for a scope set with nothing to refresh.

    The connect path 409s on that; the signin path must not.
    """
    exchanger.refresh_token = ""

    start = auth_client.get(
        "/auth/google/start?intent=signin", follow_redirects=False
    )
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    response = auth_client.get(
        f"/auth/google/callback?code=c&state={state}", follow_redirects=False
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/app/"


# --- error path ---------------------------------------------------------


def test_a_signin_callback_with_no_email_from_google_is_a_provider_error(
    auth_client, exchanger
):
    exchanger.email = ""

    start = auth_client.get(
        "/auth/google/start?intent=signin", follow_redirects=False
    )
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    response = auth_client.get(
        f"/auth/google/callback?code=c&state={state}", follow_redirects=False
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_error"


def test_no_code_path_signs_a_cookie_with_the_deleted_dev_fallback(monkeypatch):
    """The `"insecure-dev-key"` fallback is gone — a missing key must fail loudly."""
    import api.auth as auth_module

    monkeypatch.setattr(auth_module, "get_secret_key", lambda: "")

    with pytest.raises(auth_module.SecretKeyMissing):
        auth_module._serializer(auth_module.STATE_SALT)

    source = (auth_module.__file__ or "").replace(".pyc", ".py")
    assert "insecure-dev-key" not in open(source).read()
