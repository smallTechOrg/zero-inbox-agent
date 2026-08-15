"""Phase 8 — the /api/account surface: envelopes, isolation, and what it never leaks.

Three things are asserted over and over here because all three are one-line
regressions away: another user's row is a `404`, no response ever carries
`refresh_token_enc` / a raw IP / a raw user-agent, and a mismatched
`confirm_email` deletes nothing.
"""

import pytest

RAW_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


@pytest.fixture
def sessions(seed, db):
    """A live session row for each seeded user; returns {tag: (sid, token)}."""
    from api.session import create_user_session, issue_session_token

    out = {}
    for tag in ("alice", "bob"):
        user_id = f"user-{tag}"
        sid = create_user_session(user_id)
        out[tag] = (sid, issue_session_token(user_id, sid))
    return out


@pytest.fixture
def as_alice(client, sessions):
    from api.session import COOKIE_NAME

    client.cookies.set(COOKIE_NAME, sessions["alice"][1])
    return client


# --- GET /api/account: envelope ----------------------------------------


def test_account_returns_the_documented_envelope(as_alice):
    response = as_alice.get("/api/account")

    assert response.status_code == 200
    body = response.json()
    assert body["error"] is None
    data = body["data"]
    assert set(data) == {"user", "connections", "sessions", "counts"}
    assert set(data["user"]) == {"id", "email", "display_name", "created_at"}
    assert data["user"]["email"] == "alice@example.com"
    assert set(data["counts"]) == {"decisions", "action_logs", "connections"}


def test_connection_rows_carry_exactly_the_documented_fields(as_alice):
    connections = as_alice.get("/api/account").json()["data"]["connections"]

    assert len(connections) == 1
    assert set(connections[0]) == {
        "id",
        "channel",
        "account_email",
        "status",
        "connected_at",
        "last_synced_at",
    }
    # Never synced yet renders as null, never as a fabricated date.
    assert connections[0]["last_synced_at"] is None


def test_session_rows_mark_the_current_device(as_alice, sessions):
    rows = as_alice.get("/api/account").json()["data"]["sessions"]

    assert set(rows[0]) == {
        "id",
        "created_at",
        "last_seen_at",
        "user_agent_summary",
        "current",
    }
    current = [r for r in rows if r["current"]]
    assert len(current) == 1
    assert current[0]["id"] == sessions["alice"][0]


def test_counts_reflect_the_signed_in_users_own_rows_only(as_alice):
    counts = as_alice.get("/api/account").json()["data"]["counts"]

    assert counts["decisions"] == 3  # alice's three, not the six in the DB
    assert counts["connections"] == 1


# --- what it must never return ------------------------------------------


def test_no_route_ever_returns_the_encrypted_refresh_token(as_alice, sessions):
    bodies = [
        as_alice.get("/api/account").text,
        as_alice.delete(f"/api/account/sessions/{sessions['alice'][0]}").text,
    ]

    for body in bodies:
        assert "refresh_token_enc" not in body
        assert "ENCRYPTED-DO-NOT-LEAK" not in body


def test_no_raw_user_agent_or_ip_appears_in_the_account_payload(client, seed, db):
    from api.session import COOKIE_NAME, issue_session_token

    client.cookies.set(COOKIE_NAME, issue_session_token("user-alice"))
    client.get("/api/me", headers={"user-agent": RAW_UA})

    body = client.get("/api/account", headers={"user-agent": RAW_UA}).text

    assert RAW_UA not in body
    assert "AppleWebKit" not in body
    assert "ip_hash" not in body
    assert "Chrome on macOS" in body  # the derived summary IS returned


# --- cross-user isolation is a 404 on every route ------------------------


def test_alice_never_sees_bobs_connection_or_session(as_alice):
    data = as_alice.get("/api/account").json()["data"]

    assert [c["id"] for c in data["connections"]] == ["conn-alice"]
    assert all("bob" not in s["id"] for s in data["sessions"])
    assert "bob@gmail.com" not in as_alice.get("/api/account").text


def test_disconnecting_another_users_connection_is_404_and_deletes_nothing(
    as_alice, db
):
    from db.models import ChannelAccount

    response = as_alice.delete("/api/account/connections/conn-bob")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert db.get(ChannelAccount, "conn-bob") is not None


def test_revoking_another_users_session_is_404_and_revokes_nothing(
    as_alice, db, sessions
):
    from db.models import UserSession

    bob_sid = sessions["bob"][0]

    response = as_alice.delete(f"/api/account/sessions/{bob_sid}")

    assert response.status_code == 404
    db.expire_all()
    assert db.get(UserSession, bob_sid).revoked_at is None


def test_revoke_all_never_touches_another_users_sessions(as_alice, db, sessions):
    from db.models import UserSession

    response = as_alice.post("/api/account/sessions/revoke-all")

    assert response.status_code == 200
    assert response.json()["data"]["revoked"] == 1
    db.expire_all()
    assert db.get(UserSession, sessions["bob"][0]).revoked_at is None


def test_every_account_route_requires_a_session(client, seed, sessions):
    assert client.get("/api/account").status_code == 401
    assert client.delete("/api/account/connections/conn-alice").status_code == 401
    assert client.delete(f"/api/account/sessions/{sessions['alice'][0]}").status_code == 401
    assert client.post("/api/account/sessions/revoke-all").status_code == 401
    assert client.request(
        "DELETE", "/api/account", json={"confirm_email": "alice@example.com"}
    ).status_code == 401


# --- disconnect ----------------------------------------------------------


def test_disconnect_deletes_the_row_and_keeps_the_triage_history(as_alice, db):
    from db.models import ChannelAccount, Decision

    response = as_alice.delete("/api/account/connections/conn-alice")

    assert response.status_code == 200
    assert response.json()["data"] == {"disconnected": True}
    db.expire_all()
    assert db.get(ChannelAccount, "conn-alice") is None
    # The audit trail is the user's, not the mailbox's.
    assert db.query(Decision).filter(Decision.user_id == "user-alice").count() == 3


def test_disconnect_makes_zero_gmail_calls_even_when_google_revocation_fails(
    as_alice, db, monkeypatch
):
    """A Google outage must never leave a stored token we cannot remove."""
    import channels.gmail.oauth as oauth_module
    from db.models import ChannelAccount

    calls: list[str] = []

    def _boom(_token):
        calls.append("revoke")
        raise RuntimeError("google is down")

    monkeypatch.setattr(oauth_module, "revoke_refresh_token", _boom)
    monkeypatch.setattr(
        "security.crypto.TokenCipher.decrypt", lambda self, blob: "plain-token"
    )

    response = as_alice.delete("/api/account/connections/conn-alice")

    assert response.status_code == 200
    db.expire_all()
    assert db.get(ChannelAccount, "conn-alice") is None
    assert calls == ["revoke"]  # the OAuth revoke endpoint — never the Gmail API


def test_disconnecting_an_unknown_connection_is_404(as_alice):
    assert as_alice.delete("/api/account/connections/nope").status_code == 404


# --- delete account ------------------------------------------------------


def _delete_account(client, email):
    return client.request("DELETE", "/api/account", json={"confirm_email": email})


def test_a_mismatched_confirm_email_is_422_and_deletes_nothing(as_alice, db):
    from db.models import Decision, User

    response = _delete_account(as_alice, "not-my-address@example.com")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    db.expire_all()
    assert db.get(User, "user-alice") is not None
    assert db.query(Decision).filter(Decision.user_id == "user-alice").count() == 3


def test_an_empty_confirm_email_is_422_and_deletes_nothing(as_alice, db):
    from db.models import User

    response = _delete_account(as_alice, "")

    assert response.status_code == 422
    db.expire_all()
    assert db.get(User, "user-alice") is not None


def test_the_correct_confirm_email_deletes_the_account(as_alice, db):
    from db.models import User

    response = _delete_account(as_alice, "alice@example.com")

    assert response.status_code == 200
    assert response.json()["data"] == {"deleted": True}
    db.expire_all()
    assert db.get(User, "user-alice") is None


def test_deleting_an_account_never_touches_another_users_rows(as_alice, db):
    from db.models import ChannelAccount, Decision, User

    _delete_account(as_alice, "alice@example.com")

    db.expire_all()
    assert db.get(User, "user-bob") is not None
    assert db.get(ChannelAccount, "conn-bob") is not None
    assert db.query(Decision).filter(Decision.user_id == "user-bob").count() == 3
