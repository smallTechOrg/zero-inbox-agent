"""Health, session cookie and /api/me — including the per-user isolation guarantee."""

def test_health_needs_no_auth_and_reports_version(client):
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["error"] is None
    assert body["data"]["status"] == "ok"
    assert body["data"]["version"]


def test_me_without_cookie_is_unauthenticated(client, seed, sign_in):
    res = client.get("/api/me")
    assert res.status_code == 401
    body = res.json()
    assert body["data"] is None
    assert body["error"]["code"] == "unauthenticated"


def test_me_with_tampered_cookie_is_unauthenticated(client, seed, sign_in):
    from api.session import COOKIE_NAME

    client.cookies.set(COOKIE_NAME, "not-a-valid-signed-token")
    res = client.get("/api/me")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "unauthenticated"


def test_me_returns_user_connections_and_settings(client, seed, sign_in):
    sign_in("user-alice")
    res = client.get("/api/me")
    assert res.status_code == 200
    data = res.json()["data"]

    assert data["user"] == {
        "id": "user-alice",
        "email": "alice@example.com",
        "display_name": "Alice",
    }
    assert [c["account_email"] for c in data["connections"]] == ["alice@gmail.com"]
    assert data["connections"][0]["status"] == "connected"
    assert data["settings"]["dry_run"] is True


def test_me_never_leaks_the_refresh_token(client, seed, sign_in):
    sign_in("user-alice")
    raw = client.get("/api/me").text
    assert "refresh_token" not in raw
    assert "ENCRYPTED-DO-NOT-LEAK" not in raw


def test_me_shows_only_the_signed_in_users_connections(client, seed, sign_in):
    sign_in("user-bob")
    data = client.get("/api/me").json()["data"]
    emails = [c["account_email"] for c in data["connections"]]
    assert emails == ["bob@gmail.com"]
    assert "alice@gmail.com" not in emails


def test_me_for_a_deleted_user_is_unauthenticated(client, seed, sign_in):
    sign_in("user-ghost")
    res = client.get("/api/me")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "unauthenticated"


def test_session_token_roundtrip_and_rejection():
    from api.session import issue_session_token, read_session_token

    token = issue_session_token("user-alice")
    assert read_session_token(token) == "user-alice"
    assert read_session_token(token + "x") is None
    assert read_session_token("") is None
