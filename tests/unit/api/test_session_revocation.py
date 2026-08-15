"""Phase 8 — a session you can see is a session you can revoke.

Covers the four behaviours spec/api.md pins on the session cookie:
revoke → 401 on replay, revoke-all, legacy `uid`-only cookies upgraded in place
(nobody is signed out by this phase), and the `last_seen_at` 60 s throttle.
"""

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def user(db):
    from db.models import User

    row = User(id="user-sessions", email="sessions@example.com", display_name="S")
    db.add(row)
    db.commit()
    return row


def _cookie(client, token: str) -> None:
    from api.session import COOKIE_NAME

    client.cookies.set(COOKIE_NAME, token)


def _reissued_cookie(response) -> str | None:
    """The `zi_session` value the response set, if it set one."""
    from api.session import COOKIE_NAME

    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{COOKIE_NAME}="):
            return header.split("=", 1)[1].split(";", 1)[0]
    return None


def _sid_of(token: str) -> str | None:
    from api.session import read_session_payload

    payload = read_session_payload(token)
    return payload["sid"] if payload else None


# --- happy path: issue, use, revoke, replay -----------------------------


def test_a_session_backed_cookie_authenticates(client, user):
    from api.session import create_user_session, issue_session_token

    sid = create_user_session(user.id)
    _cookie(client, issue_session_token(user.id, sid))

    assert client.get("/api/me").status_code == 200


def test_a_revoked_session_replayed_is_401(client, user):
    from api.session import create_user_session, issue_session_token, revoke_session

    sid = create_user_session(user.id)
    token = issue_session_token(user.id, sid)
    _cookie(client, token)
    assert client.get("/api/me").status_code == 200

    revoke_session(sid, user.id)

    _cookie(client, token)  # the *same* cookie, replayed
    response = client.get("/api/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_logout_revokes_the_row_so_a_replayed_cookie_is_401(client, user):
    from api.session import create_user_session, issue_session_token

    sid = create_user_session(user.id)
    token = issue_session_token(user.id, sid)
    _cookie(client, token)

    assert client.post("/auth/logout").status_code == 200

    _cookie(client, token)
    assert client.get("/api/me").status_code == 401


def test_revoke_all_ends_every_session_including_the_current_one(client, db, user):
    from api.session import create_user_session, issue_session_token, revoke_all_sessions

    tokens = [issue_session_token(user.id, create_user_session(user.id)) for _ in range(3)]

    assert revoke_all_sessions(user.id) == 3

    for token in tokens:
        _cookie(client, token)
        assert client.get("/api/me").status_code == 401


def test_an_unknown_sid_is_401_even_though_the_signature_is_valid(client, user):
    from api.session import issue_session_token

    _cookie(client, issue_session_token(user.id, "no-such-session-row"))

    assert client.get("/api/me").status_code == 401


def test_a_sid_belonging_to_another_user_is_401(client, db, user):
    from db.models import User

    from api.session import create_user_session, issue_session_token

    other = User(id="user-other", email="other@example.com", display_name="O")
    db.add(other)
    db.commit()
    other_sid = create_user_session(other.id)

    # Signature is valid — the payload is ours — but the sid is not.
    _cookie(client, issue_session_token(user.id, other_sid))

    assert client.get("/api/me").status_code == 401


# --- legacy cookies are upgraded in place, never signed out -------------


def test_a_legacy_uid_only_cookie_still_authenticates(client, user):
    from api.session import issue_session_token

    _cookie(client, issue_session_token(user.id))  # no sid — pre-Phase-8 shape

    assert client.get("/api/me").status_code == 200


def test_a_legacy_cookie_gains_a_user_sessions_row_and_a_reissued_cookie(
    client, db, user
):
    from db.models import UserSession

    from api.session import issue_session_token

    legacy = issue_session_token(user.id)
    assert _sid_of(legacy) is None
    _cookie(client, legacy)

    response = client.get("/api/me")

    rows = db.query(UserSession).filter(UserSession.user_id == user.id).all()
    assert len(rows) == 1

    reissued = _reissued_cookie(response)
    assert reissued and reissued != legacy
    assert _sid_of(reissued) == rows[0].id


def test_replaying_the_same_legacy_cookie_does_not_mint_a_row_per_request(
    client, db, user
):
    from db.models import UserSession

    from api.session import issue_session_token

    legacy = issue_session_token(user.id)
    for _ in range(4):
        _cookie(client, legacy)
        assert client.get("/api/me").status_code == 200

    rows = db.query(UserSession).filter(UserSession.user_id == user.id).all()
    assert len(rows) == 1


# --- last_seen_at throttling --------------------------------------------


def test_last_seen_at_is_not_rewritten_on_every_request(client, db, user):
    from db.models import UserSession

    from api.session import create_user_session, issue_session_token

    sid = create_user_session(user.id)
    _cookie(client, issue_session_token(user.id, sid))

    client.get("/api/me")
    db.expire_all()
    first = db.get(UserSession, sid).last_seen_at

    for _ in range(3):
        client.get("/api/me")
    db.expire_all()
    assert db.get(UserSession, sid).last_seen_at == first


def test_last_seen_at_is_refreshed_once_the_throttle_window_has_passed(
    client, db, user
):
    from db.models import UserSession

    from api.session import (
        LAST_SEEN_THROTTLE_SECONDS,
        create_user_session,
        issue_session_token,
    )

    assert LAST_SEEN_THROTTLE_SECONDS == 60

    sid = create_user_session(user.id)
    stale = datetime.now(timezone.utc) - timedelta(seconds=LAST_SEEN_THROTTLE_SECONDS + 5)
    row = db.get(UserSession, sid)
    row.last_seen_at = stale
    db.commit()

    _cookie(client, issue_session_token(user.id, sid))
    client.get("/api/me")

    db.expire_all()
    refreshed = db.get(UserSession, sid).last_seen_at
    if refreshed.tzinfo is None:
        refreshed = refreshed.replace(tzinfo=timezone.utc)
    assert refreshed > stale


# --- cookie hardening ----------------------------------------------------


def test_the_cookie_is_not_marked_secure_over_plain_http(user):
    from fastapi import Request, Response

    from api.session import set_session_cookie

    request = Request({"type": "http", "scheme": "http", "path": "/", "headers": [], "method": "GET"})
    response = Response()
    set_session_cookie(response, user.id, "sid-1", request=request)

    assert "secure" not in response.headers["set-cookie"].lower()


def test_the_cookie_is_marked_secure_over_https(user):
    from fastapi import Request, Response

    from api.session import set_session_cookie

    request = Request({"type": "http", "scheme": "https", "path": "/", "headers": [], "method": "GET"})
    response = Response()
    set_session_cookie(response, user.id, "sid-1", request=request)

    header = response.headers["set-cookie"].lower()
    assert "secure" in header
    assert "httponly" in header
    assert "samesite=lax" in header


def test_no_raw_user_agent_or_ip_is_stored_on_the_session_row(client, db, user):
    from db.models import UserSession

    from api.session import issue_session_token

    raw_ua = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    _cookie(client, issue_session_token(user.id))
    client.get("/api/me", headers={"user-agent": raw_ua})

    row = db.query(UserSession).filter(UserSession.user_id == user.id).one()
    assert row.user_agent_summary == "Chrome on macOS"
    assert raw_ua not in row.user_agent_summary
    # The ip_hash is an HMAC, never the address itself.
    assert row.ip_hash is None or "testclient" not in row.ip_hash
