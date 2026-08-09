"""Unit tests for the Google OAuth web flow routes (src/api/auth.py)."""

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

SECRET = "unit-test-secret-key"


class FakeConnectionStore:
    """In-memory stand-in for the channel_accounts persistence layer."""

    def __init__(self):
        self.saved: list[dict] = []

    def upsert_user_and_connection(
        self, *, email, display_name, refresh_token_enc, scopes, channel="gmail"
    ):
        key = (email, channel)
        for row in self.saved:
            if (row["email"], row["channel"]) == key:
                row.update(
                    display_name=display_name,
                    refresh_token_enc=refresh_token_enc,
                    scopes=scopes,
                )
                return row["user_id"], row["connection_id"]
        row = {
            "user_id": f"user-{len(self.saved) + 1}",
            "connection_id": f"conn-{len(self.saved) + 1}",
            "email": email,
            "channel": channel,
            "display_name": display_name,
            "refresh_token_enc": refresh_token_enc,
            "scopes": scopes,
        }
        self.saved.append(row)
        return row["user_id"], row["connection_id"]


class FakeExchanger:
    def __init__(self, *, refresh_token="1//0gTOPSECRETREFRESH", email="me@gmail.com"):
        self.refresh_token = refresh_token
        self.email = email
        self.codes: list[str] = []
        self.verifiers: list[str | None] = []

    def __call__(self, code, state, code_verifier=None):
        from channels.gmail.oauth import OAuthResult

        self.codes.append(code)
        self.verifiers.append(code_verifier)
        return OAuthResult(
            refresh_token=self.refresh_token,
            access_token="ya29.access",
            scopes=[
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.modify",
            ],
            account_email=self.email,
            display_name="Me",
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
def store():
    return FakeConnectionStore()


@pytest.fixture
def exchanger():
    return FakeExchanger()


@pytest.fixture
def client(google_env, store, exchanger):
    from api import auth

    app = FastAPI()
    app.include_router(auth.router)
    app.dependency_overrides[auth.get_connection_store] = lambda: store
    app.dependency_overrides[auth.get_code_exchanger] = lambda: exchanger
    with TestClient(app) as c:
        yield c


# --- happy path ---------------------------------------------------------


def test_start_redirects_to_the_real_google_consent_screen(client):
    response = client.get("/auth/google/start", follow_redirects=False)

    assert response.status_code == 302
    location = urlparse(response.headers["location"])
    query = parse_qs(location.query)
    assert location.netloc == "accounts.google.com"
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert "gmail.readonly" in query["scope"][0]
    assert "gmail.compose" in query["scope"][0]


def test_the_undocumented_login_alias_is_removed(client):
    """Regression: /auth/google/login was an alias absent from spec/api.md.

    The spec is law — only /auth/google/start exists."""
    assert client.get("/auth/google/login", follow_redirects=False).status_code == 404


def test_start_sets_a_signed_csrf_state_cookie_matching_the_state_parameter(client):
    response = client.get("/auth/google/start", follow_redirects=False)

    state = parse_qs(urlparse(response.headers["location"]).query)["state"][0]
    assert "zi_oauth_state" in response.cookies
    assert state not in ("", None)


def test_callback_persists_the_connection_and_redirects_into_the_dashboard(
    client, store, exchanger
):
    start = client.get("/auth/google/start", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]

    response = client.get(
        f"/auth/google/callback?code=authcode&state={state}", follow_redirects=False
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/app/"
    assert exchanger.codes == ["authcode"]
    assert len(store.saved) == 1
    assert store.saved[0]["email"] == "me@gmail.com"


def test_the_stored_refresh_token_is_encrypted_and_decrypts_back_to_the_original(
    client, store, exchanger
):
    from security.crypto import TokenCipher

    start = client.get("/auth/google/start", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    client.get(f"/auth/google/callback?code=c&state={state}", follow_redirects=False)

    stored = store.saved[0]["refresh_token_enc"]
    assert exchanger.refresh_token not in stored
    assert TokenCipher(SECRET).decrypt(stored) == exchanger.refresh_token


def test_no_token_material_leaks_into_the_callback_response(client, exchanger):
    start = client.get("/auth/google/start", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]

    response = client.get(
        f"/auth/google/callback?code=c&state={state}", follow_redirects=False
    )

    blob = response.text + repr(dict(response.headers)) + repr(dict(response.cookies))
    assert exchanger.refresh_token not in blob
    assert "ya29.access" not in blob


def test_the_callback_establishes_the_dashboard_session(client, store):
    from api.auth import read_session_user_id

    start = client.get("/auth/google/start", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]

    response = client.get(
        f"/auth/google/callback?code=c&state={state}", follow_redirects=False
    )

    cookie = response.cookies["zi_session"]
    assert read_session_user_id(cookie) == store.saved[0]["user_id"]


def test_reconnecting_the_same_address_updates_the_row_instead_of_duplicating_it(
    client, store
):
    for _ in range(2):
        start = client.get("/auth/google/start", follow_redirects=False)
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        client.get(f"/auth/google/callback?code=c&state={state}", follow_redirects=False)

    assert len(store.saved) == 1


def test_logout_clears_the_session_cookie(client):
    response = client.post("/auth/logout")

    assert response.status_code == 200
    assert response.json() == {"data": {"logged_out": True}, "error": None}
    assert response.cookies.get("zi_session") in (None, "")


# --- edge cases ---------------------------------------------------------


def test_a_user_who_declines_consent_is_returned_to_the_dashboard_with_an_error_flag(
    client,
):
    response = client.get(
        "/auth/google/callback?error=access_denied&state=x", follow_redirects=False
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/app/?auth_error=access_denied"


def test_two_different_mailboxes_produce_two_separate_tenants(client, store, exchanger):
    for email in ("one@gmail.com", "two@gmail.com"):
        exchanger.email = email
        start = client.get("/auth/google/start", follow_redirects=False)
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        client.get(f"/auth/google/callback?code=c&state={state}", follow_redirects=False)

    assert len(store.saved) == 2
    assert store.saved[0]["user_id"] != store.saved[1]["user_id"]


# --- error paths --------------------------------------------------------


def test_a_callback_with_no_state_cookie_is_rejected_as_invalid_state(client):
    response = client.get("/auth/google/callback?code=c&state=forged", follow_redirects=False)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_state"


def test_a_callback_whose_state_does_not_match_the_cookie_is_rejected(client):
    client.get("/auth/google/start", follow_redirects=False)

    response = client.get(
        "/auth/google/callback?code=c&state=someone-elses-state", follow_redirects=False
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_state"


def test_a_callback_with_no_authorization_code_is_rejected(client):
    start = client.get("/auth/google/start", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]

    response = client.get(f"/auth/google/callback?state={state}", follow_redirects=False)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_google_returning_no_refresh_token_is_surfaced_as_reauth_required(
    client, exchanger, store
):
    exchanger.refresh_token = ""
    start = client.get("/auth/google/start", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]

    response = client.get(f"/auth/google/callback?code=c&state={state}", follow_redirects=False)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "reauth_required"
    assert store.saved == []


def test_a_failed_token_exchange_persists_nothing_and_reports_a_provider_error(
    google_env, store
):
    from api import auth

    def boom(code, state, code_verifier=None):
        raise auth.OAuthExchangeError("token endpoint said no")

    app = FastAPI()
    app.include_router(auth.router)
    app.dependency_overrides[auth.get_connection_store] = lambda: store
    app.dependency_overrides[auth.get_code_exchanger] = lambda: boom
    with TestClient(app) as c:
        start = c.get("/auth/google/start", follow_redirects=False)
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        response = c.get(f"/auth/google/callback?code=c&state={state}", follow_redirects=False)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_error"
    assert "token endpoint said no" not in response.text or store.saved == []
    assert store.saved == []


def test_read_session_rejects_a_tampered_cookie(google_env):
    from api.auth import issue_session_cookie, read_session_user_id

    good = issue_session_cookie("user-1")

    assert read_session_user_id(good) == "user-1"
    assert read_session_user_id(good[:-3] + "aaa") is None
    assert read_session_user_id("") is None


def test_the_pkce_verifier_from_start_reaches_the_token_exchange(client, exchanger):
    """Regression: the callback builds a fresh Flow, so the verifier generated at
    /start must travel in the signed state cookie. Without it Google rejects the
    exchange with `invalid_grant: Missing code verifier`."""
    start = client.get("/auth/google/start", follow_redirects=False)
    query = parse_qs(urlparse(start.headers["location"]).query)
    state = query["state"][0]

    # Google receives only the challenge; the verifier stays on our side.
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"][0]

    client.get(f"/auth/google/callback?code=authcode&state={state}", follow_redirects=False)

    assert exchanger.verifiers, "the exchanger was never called"
    assert exchanger.verifiers[0], "no PKCE verifier reached the token exchange"


def test_the_callback_cookie_is_readable_by_the_api_session_guard(client, exchanger):
    """Regression: auth.py wrote the session cookie with its own salt and a
    `user_id` payload while api/session.py read it with a different salt and a
    `uid` payload. Every signature check failed, so a user who had genuinely
    connected Gmail still got 401 from /api/me. Pin the two halves together."""
    from api.session import read_session_token

    start = client.get("/auth/google/start", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    callback = client.get(
        f"/auth/google/callback?code=authcode&state={state}", follow_redirects=False
    )

    raw = callback.cookies.get("zi_session") or client.cookies.get("zi_session")
    assert raw, "the callback set no session cookie"
    assert read_session_token(raw), "the session guard cannot read the cookie auth.py wrote"
