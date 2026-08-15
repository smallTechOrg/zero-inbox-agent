"""Seam: auth → db. Session cookie ↔ user row ↔ gmail_account status.

The historic defect class here is "plumbed but never wired" — an endpoint that
answers without ever consulting the session or the DB. Each test fails if the
seam is cut.
"""

from __future__ import annotations

import pytest

from tests.conftest import seed_user, session_cookie_for
from tests.fixtures.fake_gmail import force_refresh_failure
from tests.integration._helpers import envelope_error, envelope_ok

pytestmark = pytest.mark.integration


class TestSignedOut:
    def test_me_signed_out_is_401_signed_out_envelope(self, api_client):
        response = api_client.get("/api/me")
        assert response.status_code == 401
        envelope_error(response, code="signed_out")

    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/api/taxonomy"),
            ("POST", "/api/audit"),
            ("GET", "/api/audit/latest"),
            ("GET", "/api/runs"),
            ("POST", "/api/runs"),
        ],
    )
    def test_every_protected_route_refuses_anonymous_calls(self, api_client, method, path):
        response = api_client.request(method, path, json={} if method == "POST" else None)
        assert response.status_code == 401, f"{method} {path} answered {response.status_code}"
        envelope_error(response, code="signed_out")

    def test_health_is_public_and_leaks_no_secret(self, api_client):
        import os

        response = api_client.get("/api/health")
        assert response.status_code == 200
        text = response.text
        for var in ("AGENT_NVIDIA_API_KEY", "AGENT_GEMINI_API_KEY", "AGENT_GOOGLE_CLIENT_SECRET", "AGENT_SECRET_KEY"):
            value = os.environ.get(var)
            if value:
                assert value not in text, f"/api/health leaked {var}"


class TestSignedIn:
    def test_me_reflects_the_seeded_user_and_connected_gmail(self, auth_client, seeded_user):
        user, _ = seeded_user
        data = envelope_ok(auth_client.get("/api/me"))
        blob = str(data)
        assert user.email in blob, f"/api/me does not reflect the DB user row: {data}"
        assert "connected" in blob, f"/api/me must expose gmail connection status: {data}"

    def test_a_forged_cookie_does_not_authenticate(self, api_client, seeded_user):
        name, value = session_cookie_for(seeded_user[0].id)
        api_client.cookies.set(name, value[:-4] + "XXXX")
        response = api_client.get("/api/me")
        assert response.status_code == 401

    def test_logout_clears_the_session(self, auth_client):
        envelope_ok(auth_client.post("/api/auth/logout"))
        response = auth_client.get("/api/me")
        assert response.status_code == 401


class TestMultiUserIsolation:
    def test_two_users_cannot_see_each_others_taxonomy(self, api_client, db_session):
        seed_user(db_session, "test-user-alice", "alice@example.com")
        seed_user(db_session, "test-user-bob", "bob@example.com")

        # Bob renames nothing; Alice adds a distinctive category.
        name, value = session_cookie_for("test-user-alice")
        api_client.cookies.set(name, value)
        envelope_ok(
            api_client.post(
                "/api/taxonomy",
                json={"name": "Alice Secret", "description": "hers", "rule": "label_only"},
            )
        )

        name, value = session_cookie_for("test-user-bob")
        api_client.cookies.set(name, value)
        bob_taxonomy = envelope_ok(api_client.get("/api/taxonomy"))
        assert "Alice Secret" not in str(bob_taxonomy), (
            "cross-user leak: Bob can see Alice's category"
        )


class TestRevokedToken:
    def test_invalid_grant_yields_gmail_reconnect_never_a_traceback(
        self, auth_client, monkeypatch, db_session, seeded_user
    ):
        force_refresh_failure(monkeypatch)
        response = auth_client.post("/api/audit")
        assert response.status_code < 500, (
            f"a revoked token must never surface as a server error: {response.status_code} {response.text[:300]}"
        )
        error = envelope_error(response, code="gmail_reconnect")
        assert "reconnect" in error["message"].lower()
        assert "Traceback" not in response.text

    def test_invalid_grant_flips_account_status_to_needs_reconnect(
        self, auth_client, monkeypatch, db_session, seeded_user
    ):
        from db import models

        force_refresh_failure(monkeypatch)
        auth_client.post("/api/audit")
        db_session.expire_all()
        account = (
            db_session.query(models.GmailAccount)
            .filter(models.GmailAccount.user_id == seeded_user[0].id)
            .one()
        )
        assert account.status == "needs_reconnect", (
            "the auth→db seam is cut: a Google invalid_grant did not flip "
            "gmail_accounts.status to needs_reconnect"
        )
