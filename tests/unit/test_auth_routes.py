"""Google sign-in routes (auth-gmail slice): login, callback, me, logout, disconnect.

Contract under test: spec/api.md Auth & Account + spec/capabilities/google-signin.md.
The Google exchange is faked at the dependency seam; everything below it —
encryption, DB rows, session cookies, per-user isolation — is real, against the
isolated tmp_path database.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import api.auth as auth
import api.session as session_mod
from api._common import error_body
from channels.gmail.oauth import OAuthResult


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("AGENT_SECRET_KEY", "unit-test-secret-key")


@pytest.fixture
def app(_isolated_db):
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(session_mod.router)

    @app.exception_handler(HTTPException)
    async def _render(request, exc):  # spec/api.md envelope for raised errors
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": "error", "message": str(exc.detail)}
        return JSONResponse(status_code=exc.status_code, content=error_body(detail["code"], detail["message"]))

    return app


@pytest.fixture
def client(app):
    return TestClient(app, follow_redirects=False)


def _result(email="alice@example.com", name="Alice", refresh="refresh-token-alice"):
    return OAuthResult(
        refresh_token=refresh,
        access_token="ya29.access",
        scopes=list(auth.google_oauth_config.__defaults__ or []) or ["openid"],
        account_email=email,
        display_name=name,
    )


def _sign_in(app, client, result: OAuthResult):
    """Drive the real login→callback flow with a faked Google exchange."""
    login = client.get("/auth/google/login")
    assert login.status_code == 302
    state_cookie = login.cookies["zi_oauth_state"]
    state = auth._serializer().loads(state_cookie)["state"]

    app.dependency_overrides[auth.get_code_exchanger] = lambda: (
        lambda code, st, verifier=None: result
    )
    client.cookies.set("zi_oauth_state", state_cookie)
    response = client.get(f"/auth/google/callback?code=4/fake-code&state={state}")
    app.dependency_overrides.pop(auth.get_code_exchanger, None)
    return response


# --- happy path -------------------------------------------------------------


def test_login_redirects_to_google_with_single_consent_scopes(client, monkeypatch):
    monkeypatch.setenv("AGENT_GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("AGENT_GOOGLE_CLIENT_SECRET", "client-secret")
    response = client.get("/auth/google/login")
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://accounts.google.com/")
    assert "gmail.modify" in location and "openid" in location
    # readonly/compose/settings are never requested
    assert "gmail.readonly" not in location and "gmail.compose" not in location
    assert "zi_oauth_state" in response.cookies


def test_callback_creates_user_encrypted_token_and_session(app, client, monkeypatch):
    monkeypatch.setenv("AGENT_GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("AGENT_GOOGLE_CLIENT_SECRET", "client-secret")
    response = _sign_in(app, client, _result())
    assert response.status_code == 302
    assert response.headers["location"] == "http://localhost:3000/"
    assert "zi_session" in response.cookies

    from db.models import GmailAccount, User
    from db.session import create_db_session
    from security.crypto import TokenCipher

    with create_db_session() as db:
        user = db.query(User).filter(User.email == "alice@example.com").one()
        account = db.query(GmailAccount).filter(GmailAccount.user_id == user.id).one()
        assert account.status == "connected"
        # encrypted at rest, decryptable with the AGENT_SECRET_KEY-derived key
        assert account.refresh_token_encrypted != "refresh-token-alice"
        assert (
            TokenCipher().decrypt(account.refresh_token_encrypted)
            == "refresh-token-alice"
        )

    # the session cookie authenticates /api/me with the spec envelope + shape
    client.cookies.set("zi_session", response.cookies["zi_session"])
    me = client.get("/api/me").json()
    assert me["ok"] is True
    assert me["data"]["user"]["email"] == "alice@example.com"
    assert me["data"]["gmail"]["status"] == "connected"
    assert me["data"]["gmail"]["email"] == "alice@example.com"
    # the token never appears in any API payload
    assert "refresh" not in str(me).lower()


def test_reconnect_state_and_disconnect_lifecycle(app, client, monkeypatch):
    monkeypatch.setenv("AGENT_GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("AGENT_GOOGLE_CLIENT_SECRET", "client-secret")
    response = _sign_in(app, client, _result())
    client.cookies.set("zi_session", response.cookies["zi_session"])

    from channels.gmail.store import SqlConnectionStore
    from db.models import User
    from db.session import create_db_session

    with create_db_session() as db:
        user_id = db.query(User.id).filter(User.email == "alice@example.com").scalar()

    # a token failure anywhere flips the surface to needs_reconnect
    assert auth.mark_needs_reconnect(user_id) is True
    assert client.get("/api/me").json()["data"]["gmail"]["status"] == "needs_reconnect"

    # disconnect deletes the row; revocation at Google is best-effort (no network)
    revoked: list[str] = []
    monkeypatch.setattr(auth, "revoke_refresh_token", lambda tok: revoked.append(tok) or True)
    body = client.post("/api/gmail/disconnect").json()
    assert body == {"ok": True, "data": {"disconnected": True}}
    assert revoked == ["refresh-token-alice"]
    assert SqlConnectionStore().connection_for(user_id) is None
    assert client.get("/api/me").json()["data"]["gmail"]["status"] == "none"


def test_logout_clears_the_session_cookie(app, client, monkeypatch):
    monkeypatch.setenv("AGENT_GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("AGENT_GOOGLE_CLIENT_SECRET", "client-secret")
    response = _sign_in(app, client, _result())
    client.cookies.set("zi_session", response.cookies["zi_session"])

    out = client.post("/api/auth/logout")
    assert out.json()["ok"] is True
    # the cookie is expired on the client
    assert 'zi_session=""' in out.headers.get("set-cookie", "")

    client.cookies.delete("zi_session")
    me = client.get("/api/me")
    assert me.status_code == 401
    assert me.json()["error"]["code"] == "signed_out"


# --- edge: multi-user isolation ---------------------------------------------


def test_two_users_never_see_each_others_connection(app, client, monkeypatch):
    monkeypatch.setenv("AGENT_GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("AGENT_GOOGLE_CLIENT_SECRET", "client-secret")
    alice = _sign_in(app, client, _result())
    bob = _sign_in(
        app, client, _result(email="bob@example.com", name="Bob", refresh="refresh-token-bob")
    )

    client.cookies.set("zi_session", alice.cookies["zi_session"])
    assert client.get("/api/me").json()["data"]["gmail"]["email"] == "alice@example.com"
    client.cookies.set("zi_session", bob.cookies["zi_session"])
    assert client.get("/api/me").json()["data"]["gmail"]["email"] == "bob@example.com"


# --- error paths ------------------------------------------------------------


def test_me_without_a_session_is_401_signed_out(client):
    response = client.get("/api/me")
    assert response.status_code == 401
    body = response.json()
    assert body["ok"] is False and body["error"]["code"] == "signed_out"


def test_callback_with_tampered_state_is_rejected(client, monkeypatch):
    monkeypatch.setenv("AGENT_GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("AGENT_GOOGLE_CLIENT_SECRET", "client-secret")
    login = client.get("/auth/google/login")
    client.cookies.set("zi_oauth_state", login.cookies["zi_oauth_state"])
    response = client.get("/auth/google/callback?code=4/fake&state=attacker-forged")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_state"


def test_callback_without_refresh_token_is_structured_gmail_reconnect(app, client, monkeypatch):
    monkeypatch.setenv("AGENT_GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("AGENT_GOOGLE_CLIENT_SECRET", "client-secret")
    response = _sign_in(app, client, _result(refresh=""))
    assert response.status_code == 409
    body = response.json()
    assert body["ok"] is False and body["error"]["code"] == "gmail_reconnect"

    from db.models import User
    from db.session import create_db_session

    with create_db_session() as db:  # nothing was persisted
        assert db.query(User).count() == 0


def test_user_declined_consent_redirects_home_with_reason_not_a_traceback(client):
    response = client.get("/auth/google/callback?error=access_denied")
    assert response.status_code == 302
    assert response.headers["location"] == "http://localhost:3000/?auth_error=access_denied"
