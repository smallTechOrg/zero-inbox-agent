"""Phase 8 identity, end to end against a real (isolated) database.

The four load-bearing claims of slice 1, each proved rather than asserted:

1. A full ``intent=signin`` creates a user and a session — and **zero**
   ``channel_accounts`` rows and **zero** triage runs. Signing in is not the same
   act as granting mailbox access, and if it silently were, the minimal-scope
   promise on the homepage would be a lie.
2. A ``connect`` for an address already owned by a different user is
   ``409 mailbox_already_connected`` and writes **zero** rows. Two agents must
   never mutate one inbox under two independent policies.
3. Account deletion removes rows from **every** user-scoped table — asserted table
   by table, driven off the schema so a table added later cannot be forgotten —
   with a spy proving **zero** Gmail calls. Archived mail stays archived.
4. Migration ``0007``'s ownership guard **raises naming the offending addresses**
   on a seeded duplicate, and creates the index when there is none. It never
   resolves the conflict: which human owns a mailbox is not a migration's call.

Every test runs against ``_isolated_db`` or a throwaway file DB. Nothing here
touches the real ``zero_inbox.db`` or the live Gmail account.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

REPO_ROOT = Path(__file__).resolve().parents[2]
SECRET = "integration-identity-secret"


# --- OAuth doubles (Google is the only thing stubbed; the DB is real) ----


class StubExchanger:
    def __init__(self, *, email, display_name="Real Person", refresh_token="1//REFRESH"):
        self.email = email
        self.display_name = display_name
        self.refresh_token = refresh_token

    def __call__(self, code, state, code_verifier=None, *, scopes=None):
        from channels.gmail.oauth import CONNECT_SCOPES, OAuthResult

        return OAuthResult(
            refresh_token=self.refresh_token,
            access_token="ya29.access",
            scopes=list(scopes) if scopes else list(CONNECT_SCOPES),
            account_email=self.email,
            display_name=self.display_name,
        )


@pytest.fixture
def google_env(monkeypatch):
    monkeypatch.setenv("AGENT_GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("AGENT_GOOGLE_CLIENT_SECRET", "csecret")
    monkeypatch.setenv(
        "AGENT_GOOGLE_REDIRECT_URI", "http://localhost:8001/auth/google/callback"
    )
    monkeypatch.setenv("AGENT_SECRET_KEY", SECRET)
    import config.settings as settings_module

    settings_module._settings = None
    yield
    settings_module._settings = None


@pytest.fixture
def db(_isolated_db):
    import db.session as session_module

    with session_module._SessionLocal() as session:
        yield session


def _oauth_app(exchanger, *, triage_spy=None, monkeypatch=None):
    from api import auth

    if triage_spy is not None and monkeypatch is not None:
        monkeypatch.setattr(auth, "_auto_triage_task", triage_spy)

    app = FastAPI()
    app.include_router(auth.router)
    app.dependency_overrides[auth.get_code_exchanger] = lambda: exchanger
    return app


def _complete_flow(client, *, intent: str | None):
    path = "/auth/google/start" + (f"?intent={intent}" if intent else "")
    start = client.get(path, follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    return client.get(
        f"/auth/google/callback?code=authcode&state={state}", follow_redirects=False
    )


# --- 1. a full sign-in writes identity and nothing else -----------------


def test_a_full_signin_creates_a_user_a_session_and_nothing_else(
    google_env, db, monkeypatch
):
    from db.models import ChannelAccount, TriageRun, User, UserSession

    triaged: list = []
    app = _oauth_app(
        StubExchanger(email="newuser@gmail.com"),
        triage_spy=lambda **kw: triaged.append(kw),
        monkeypatch=monkeypatch,
    )

    with TestClient(app) as client:
        response = _complete_flow(client, intent="signin")

    assert response.status_code == 302
    assert response.headers["location"] == "/app/"

    user = db.query(User).filter(User.email == "newuser@gmail.com").one()
    assert user.display_name == "Real Person"

    assert db.query(UserSession).filter(UserSession.user_id == user.id).count() == 1
    # The two that must be zero.
    assert db.query(ChannelAccount).count() == 0
    assert db.query(TriageRun).count() == 0
    assert triaged == []


def test_signing_in_twice_reuses_the_one_user_and_rotates_the_session(
    google_env, db, monkeypatch
):
    from db.models import User, UserSession

    app = _oauth_app(
        StubExchanger(email="repeat@gmail.com"),
        triage_spy=lambda **kw: None,
        monkeypatch=monkeypatch,
    )

    with TestClient(app) as client:
        _complete_flow(client, intent="signin")
        _complete_flow(client, intent="signin")

    assert db.query(User).filter(User.email == "repeat@gmail.com").count() == 1
    # Token rotation on every sign-in: a second row, not a reused one.
    assert db.query(UserSession).count() == 2


def test_a_signed_in_user_with_no_mailbox_reaches_the_api_and_has_no_connection(
    google_env, db, monkeypatch
):
    from api import app as real_app
    from api.session import COOKIE_NAME

    oauth = _oauth_app(
        StubExchanger(email="nomailbox@gmail.com"),
        triage_spy=lambda **kw: None,
        monkeypatch=monkeypatch,
    )
    with TestClient(oauth) as client:
        response = _complete_flow(client, intent="signin")
    cookie = response.cookies["zi_session"]

    with TestClient(real_app) as api:
        api.cookies.set(COOKIE_NAME, cookie)
        me = api.get("/api/me")
        account = api.get("/api/account")

    assert me.status_code == 200
    assert me.json()["data"]["connections"] == []
    assert account.json()["data"]["connections"] == []


# --- 2. global mailbox ownership ----------------------------------------


def test_connecting_a_mailbox_owned_by_another_user_is_409_and_writes_nothing(
    google_env, db, monkeypatch
):
    from db.models import ChannelAccount, User

    # A different Zero Inbox user already owns shared@gmail.com.
    db.add(User(id="owner-1", email="owner@example.com", display_name="Owner"))
    db.add(
        ChannelAccount(
            id="conn-owner",
            user_id="owner-1",
            channel="gmail",
            account_email="shared@gmail.com",
            refresh_token_enc="ENCRYPTED",
            scopes=["gmail.modify"],
            status="connected",
        )
    )
    db.commit()

    users_before = db.query(User).count()
    accounts_before = db.query(ChannelAccount).count()

    triaged: list = []
    app = _oauth_app(
        StubExchanger(email="shared@gmail.com"),
        triage_spy=lambda **kw: triaged.append(kw),
        monkeypatch=monkeypatch,
    )
    with TestClient(app) as client:
        response = _complete_flow(client, intent="connect")

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "mailbox_already_connected"
    assert "shared@gmail.com" in body["error"]["message"]

    db.expire_all()
    assert db.query(User).count() == users_before
    assert db.query(ChannelAccount).count() == accounts_before
    assert db.get(ChannelAccount, "conn-owner").user_id == "owner-1"
    assert triaged == []


def test_reconnecting_your_own_mailbox_still_succeeds_idempotently(
    google_env, db, monkeypatch
):
    from db.models import ChannelAccount

    app = _oauth_app(
        StubExchanger(email="mine@gmail.com"),
        triage_spy=lambda **kw: None,
        monkeypatch=monkeypatch,
    )
    with TestClient(app) as client:
        first = _complete_flow(client, intent="connect")
        second = _complete_flow(client, intent="connect")

    assert first.status_code == 302 and second.status_code == 302
    db.expire_all()
    assert db.query(ChannelAccount).count() == 1


def test_connecting_a_mailbox_schedules_the_first_triage_run(
    google_env, db, monkeypatch
):
    """The positive mirror of the sign-in test: `intent=connect` MUST schedule triage.

    Without this, the `add_task` on the connect path could be deleted and the whole
    identity slice would stay green — and the first thing a brand-new user would
    experience is "I connected my mailbox and nothing happened".
    """
    from db.models import ChannelAccount, User

    triaged: list = []
    app = _oauth_app(
        StubExchanger(email="firstrun@gmail.com"),
        triage_spy=lambda **kw: triaged.append(kw),
        monkeypatch=monkeypatch,
    )

    with TestClient(app) as client:
        response = _complete_flow(client, intent="connect")

    assert response.status_code == 302
    db.expire_all()
    user = db.query(User).filter(User.email == "firstrun@gmail.com").one()
    connection = db.query(ChannelAccount).filter(ChannelAccount.user_id == user.id).one()

    # Exactly one background triage, for exactly this user and this connection.
    assert triaged == [{"user_id": user.id, "connection_id": connection.id}]


def test_the_schema_itself_refuses_a_second_owner_for_one_mailbox(db):
    """Belt and braces: the route guard is backed by a real unique index."""
    from sqlalchemy.exc import IntegrityError

    from db.models import ChannelAccount, User

    for tag in ("a", "b"):
        db.add(User(id=f"user-{tag}", email=f"{tag}@example.com", display_name=tag))
    db.commit()

    for tag in ("a", "b"):
        db.add(
            ChannelAccount(
                id=f"conn-{tag}",
                user_id=f"user-{tag}",
                channel="gmail",
                account_email="one-inbox@gmail.com",
                refresh_token_enc="E",
                status="connected",
            )
        )

    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# --- 3. account deletion: every table, zero Gmail calls ------------------


@pytest.fixture
def populated_user(db):
    """One user with a row in every user-scoped table this schema has."""
    from datetime import datetime, timezone

    from db.models import (
        ActionLog,
        Category,
        ChannelAccount,
        Cluster,
        Correction,
        Decision,
        Item,
        LLMCall,
        PriorityProfile,
        Rule,
        SenderProfile,
        TriageRun,
        User,
        UserSession,
        UserSettings,
        VipEntry,
    )

    now = datetime.now(timezone.utc)
    uid = "doomed-user"
    db.add(User(id=uid, email="doomed@example.com", display_name="Doomed"))
    db.add(UserSettings(user_id=uid))
    db.add(UserSession(id="sess-doomed", user_id=uid, created_at=now, last_seen_at=now))
    db.add(
        ChannelAccount(
            id="conn-doomed",
            user_id=uid,
            channel="gmail",
            account_email="doomed@gmail.com",
            refresh_token_enc="ENCRYPTED",
            status="connected",
        )
    )
    db.add(Category(id="cat-doomed", user_id=uid, key="newsletters", name="Newsletters"))
    db.add(Rule(id="rule-doomed", user_id=uid, name="R", matcher={}, action={}))
    db.add(
        TriageRun(
            id="run-doomed",
            user_id=uid,
            channel_account_id="conn-doomed",
            status="completed",
            counts={},
            started_at=now,
        )
    )
    db.add(Cluster(id="clu-doomed", user_id=uid, run_id="run-doomed"))
    db.add(
        Item(
            id="item-doomed",
            user_id=uid,
            channel_account_id="conn-doomed",
            external_thread_id="t1",
            snippet_redacted="redacted",
        )
    )
    db.add(
        Decision(
            id="dec-doomed",
            user_id=uid,
            item_id="item-doomed",
            run_id="run-doomed",
            decided_by="llm",
            review_state="reviewed",
        )
    )
    db.add(
        ActionLog(
            id="log-doomed",
            user_id=uid,
            decision_id="dec-doomed",
            operation="archive",
            request_params={},
        )
    )
    db.add(
        Correction(
            id="cor-doomed",
            user_id=uid,
            item_id="item-doomed",
            from_action="archive",
            to_action="keep",
        )
    )
    db.add(LLMCall(id="llm-doomed", user_id=uid, purpose="classify", model="m"))
    db.add(SenderProfile(id="sp-doomed", user_id=uid, sender_email="s@x.com"))
    db.add(VipEntry(id="vip-doomed", user_id=uid, kind="email", value="v@x.com"))
    db.add(PriorityProfile(user_id=uid, text="my priorities"))
    db.commit()
    return uid


def _user_scoped_table_names() -> list[str]:
    from db.models import Base

    return [
        t.name for t in Base.metadata.sorted_tables if "user_id" in t.c and t.name != "users"
    ]


class GmailCallSpy:
    """Fails the test if anything reaches the Gmail API surface."""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, *args, **kwargs):
        self.calls.append("gmail")
        raise AssertionError("account deletion made a Gmail API call")


def test_deleting_an_account_removes_rows_from_every_user_scoped_table(
    google_env, db, populated_user, monkeypatch
):
    import googleapiclient.discovery

    from api import app as real_app
    from api.session import COOKIE_NAME, create_user_session, issue_session_token
    from db.models import User

    spy = GmailCallSpy()
    monkeypatch.setattr(googleapiclient.discovery, "build", spy)

    tables = _user_scoped_table_names()
    assert len(tables) >= 15, "the schema shrank — re-check this assertion"
    for name in tables:
        count = db.execute(
            text(f"SELECT COUNT(*) FROM {name} WHERE user_id = :u"), {"u": populated_user}
        ).scalar_one()
        assert count > 0, f"{name} was not seeded — the deletion proof would be vacuous"

    sid = create_user_session(populated_user)
    with TestClient(real_app) as client:
        client.cookies.set(COOKIE_NAME, issue_session_token(populated_user, sid))
        response = client.request(
            "DELETE", "/api/account", json={"confirm_email": "doomed@example.com"}
        )

    assert response.status_code == 200
    assert response.json()["data"] == {"deleted": True}

    db.expire_all()
    for name in tables:
        remaining = db.execute(
            text(f"SELECT COUNT(*) FROM {name} WHERE user_id = :u"), {"u": populated_user}
        ).scalar_one()
        assert remaining == 0, f"{name} still holds rows for the deleted user"
    assert db.get(User, populated_user) is None

    # Archived mail stays archived: not one Gmail operation was performed.
    assert spy.calls == []


def test_a_deleted_account_can_no_longer_authenticate(
    google_env, db, populated_user
):
    from api import app as real_app
    from api.session import COOKIE_NAME, create_user_session, issue_session_token

    sid = create_user_session(populated_user)
    token = issue_session_token(populated_user, sid)

    with TestClient(real_app) as client:
        client.cookies.set(COOKIE_NAME, token)
        client.request(
            "DELETE", "/api/account", json={"confirm_email": "doomed@example.com"}
        )
        client.cookies.set(COOKIE_NAME, token)
        replay = client.get("/api/account")

    assert replay.status_code == 401


# --- 4. migration 0007 ---------------------------------------------------


@pytest.fixture
def at_0006(tmp_path, monkeypatch):
    """A throwaway file DB upgraded to 0006 — never the real zero_inbox.db."""
    db_path = tmp_path / "phase8.db"
    monkeypatch.setenv("AGENT_DATABASE_URL", f"sqlite:///{db_path}")
    import config.settings as settings_module

    settings_module._settings = None

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    command.upgrade(cfg, "0006_autonomy_policy")

    engine = create_engine(f"sqlite:///{db_path}")
    yield cfg, engine
    engine.dispose()


def _seed_account(engine, *, user_id, email, account_email, connection_id):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, email, display_name, created_at) "
                "VALUES (:id, :email, :name, '2026-01-01T00:00:00')"
            ),
            {"id": user_id, "email": email, "name": user_id},
        )
        conn.execute(
            text(
                "INSERT INTO channel_accounts (id, user_id, channel, account_email, "
                "refresh_token_enc, status, connected_at) VALUES (:id, :u, 'gmail', "
                ":ae, 'ENC', 'connected', '2026-01-01T00:00:00')"
            ),
            {"id": connection_id, "u": user_id, "ae": account_email},
        )


def test_0007_creates_the_session_table_the_index_and_last_synced_at(at_0006):
    cfg, engine = at_0006
    _seed_account(
        engine,
        user_id="u1",
        email="one@x.com",
        account_email="one@gmail.com",
        connection_id="c1",
    )

    command.upgrade(cfg, "0007_sessions_and_mailbox_ownership")

    inspector = inspect(engine)
    assert "user_sessions" in inspector.get_table_names()

    columns = {c["name"] for c in inspector.get_columns("user_sessions")}
    assert columns == {
        "id",
        "user_id",
        "created_at",
        "last_seen_at",
        "revoked_at",
        "user_agent_summary",
        "ip_hash",
    }

    session_indexes = {i["name"] for i in inspector.get_indexes("user_sessions")}
    assert "ix_user_sessions_user_active" in session_indexes

    account_indexes = {
        i["name"]: i for i in inspector.get_indexes("channel_accounts")
    }
    assert bool(account_indexes["uq_channel_account_global"]["unique"])
    assert account_indexes["uq_channel_account_global"]["column_names"] == [
        "channel",
        "account_email",
    ]

    assert "last_synced_at" in {
        c["name"] for c in inspector.get_columns("channel_accounts")
    }
    # No backfill: NULL renders as "not synced yet", never a fabricated date.
    with engine.connect() as conn:
        assert conn.execute(text("SELECT last_synced_at FROM channel_accounts")).scalar_one() is None


def test_0007_raises_naming_the_offending_addresses_on_a_duplicate(at_0006):
    cfg, engine = at_0006
    _seed_account(
        engine,
        user_id="u1",
        email="one@x.com",
        account_email="contested@gmail.com",
        connection_id="c1",
    )
    _seed_account(
        engine,
        user_id="u2",
        email="two@x.com",
        account_email="contested@gmail.com",
        connection_id="c2",
    )

    with pytest.raises(Exception) as excinfo:
        command.upgrade(cfg, "0007_sessions_and_mailbox_ownership")

    message = str(excinfo.value)
    assert "contested@gmail.com" in message
    assert "will not choose for you" in message


def test_a_refused_0007_never_reassigns_or_deletes_a_row(at_0006):
    """Which human owns a mailbox is not a decision a migration gets to make."""
    cfg, engine = at_0006
    _seed_account(
        engine,
        user_id="u1",
        email="one@x.com",
        account_email="contested@gmail.com",
        connection_id="c1",
    )
    _seed_account(
        engine,
        user_id="u2",
        email="two@x.com",
        account_email="contested@gmail.com",
        connection_id="c2",
    )

    with pytest.raises(Exception):
        command.upgrade(cfg, "0007_sessions_and_mailbox_ownership")

    with engine.connect() as conn:
        owners = conn.execute(
            text("SELECT id, user_id FROM channel_accounts ORDER BY id")
        ).all()
    assert [tuple(r) for r in owners] == [("c1", "u1"), ("c2", "u2")]


def test_0007_downgrades_cleanly(at_0006):
    cfg, engine = at_0006
    _seed_account(
        engine,
        user_id="u1",
        email="one@x.com",
        account_email="one@gmail.com",
        connection_id="c1",
    )

    command.upgrade(cfg, "0007_sessions_and_mailbox_ownership")
    command.downgrade(cfg, "0006_autonomy_policy")

    inspector = inspect(engine)
    assert "user_sessions" not in inspector.get_table_names()
    assert "last_synced_at" not in {
        c["name"] for c in inspector.get_columns("channel_accounts")
    }
    assert "uq_channel_account_global" not in {
        i["name"] for i in inspector.get_indexes("channel_accounts")
    }
    # The rows it was never allowed to touch are still exactly as they were.
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM channel_accounts")).scalar_one() == 1
