"""Suite-wide fixtures + the test-isolation guard (Zero Inbox redesign, Phase 1).

THE GUARD (spec/architecture.md § Test-Isolation Guard) — four independent
layers, none of which has an opt-out:

1. ``AGENT_TEST_ISOLATION=1`` is exported before any ``src`` module can be
   imported. CROSS-SLICE CONTRACT: the Gmail mutation choke point in
   ``src/channels/gmail/mutations.py`` must, when this flag is set, write the
   audit row and return the mutation as applied WITHOUT calling Gmail's write
   API. Reads and LLM calls stay real (keys from ``.env``).
2. Network backstop: ``googleapiclient``'s ``HttpRequest.execute`` is wrapped —
   any non-GET request to the Gmail API raises ``RealGmailError`` before it is
   sent. This holds even if the choke point ignores the flag.
3. DB binding: every test runs against a throwaway SQLite file under
   ``tmp_path``; ``assert_isolated_db`` refuses ``agent.db`` / ``data/`` /
   anything outside the pytest temp root.
4. Row guard: a ``before_flush`` listener on the ``Session`` CLASS refuses any
   guarded-table row naming the real account or real mailbox, whatever engine
   the code under test built for itself.

Test data convention: user ids start ``test-`` (e.g. ``test-user-alice``);
mailboxes end ``@example.com`` / ``@test.invalid``. See tests/isolation.py.
"""

from __future__ import annotations

import os

# Layer 1: exported before any src import (pytest imports conftest first).
os.environ["AGENT_TEST_ISOLATION"] = "1"

import pytest  # noqa: E402

from tests.isolation import (  # noqa: E402
    GUARDED_TABLES,
    assert_isolated_db,
    assert_not_real_gmail_write,
    assert_row_not_real,
)

# --------------------------------------------------------------------------
# Settings hygiene
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_settings_singleton():
    """A cached Settings object must never leak one test's env into the next.

    (The cached-``get_settings()`` incident is how a script once reached the
    real database.) Tolerates the module not existing yet mid-rebuild.
    """
    try:
        import config.settings as m
    except ImportError:
        yield
        return
    for attr in ("_settings", "_SETTINGS"):
        if hasattr(m, attr):
            setattr(m, attr, None)
    yield
    for attr in ("_settings", "_SETTINGS"):
        if hasattr(m, attr):
            setattr(m, attr, None)


# --------------------------------------------------------------------------
# Layer 3: isolated database
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Bind the whole app to a throwaway SQLite file under tmp_path.

    Belt and braces: the env var steers settings-driven binding, and the
    ``db.session`` module's engine/factory globals are re-pointed directly.
    """
    db_file = tmp_path / "test.db"
    url = f"sqlite:///{db_file}"
    monkeypatch.setenv("AGENT_DATABASE_URL", url)
    monkeypatch.setenv("DATABASE_URL", url)

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from db.models import Base
    import db.session as session_module

    engine = create_engine(url)
    # Structural proof the binding is throwaway BEFORE a single row is written.
    assert_isolated_db(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    bound = False
    for name, value in (
        ("_engine", engine),
        ("engine", engine),
        ("_SessionLocal", factory),
        ("SessionLocal", factory),
        ("_session_factory", factory),
    ):
        if hasattr(session_module, name):
            monkeypatch.setattr(session_module, name, value)
            bound = True
    if hasattr(session_module, "init_db"):
        monkeypatch.setattr(session_module, "init_db", lambda: None)
    if not bound:
        pytest.fail(
            "test-isolation contract: db/session.py must expose its engine and "
            "session factory as module globals (e.g. _engine/_SessionLocal) so "
            "the suite can re-point them at a throwaway tmp_path database."
        )
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(_isolated_db):
    """A plain ORM session on the isolated engine, for seeding and asserting."""
    from sqlalchemy.orm import Session

    with Session(_isolated_db) as session:
        yield session


# --------------------------------------------------------------------------
# Layer 4: the row-level write guard (Session class, no opt-out)
# --------------------------------------------------------------------------


def _guard_flush(session, _flush_context, _instances):
    guarded = [
        obj
        for obj in list(session.new) + list(session.dirty)
        if getattr(getattr(obj, "__table__", None), "name", None) in GUARDED_TABLES
    ]
    if not guarded:
        return
    assert_isolated_db(session.get_bind())
    for obj in guarded:
        assert_row_not_real(obj, table=obj.__table__.name)


@pytest.fixture(autouse=True)
def _no_real_account():
    """No test may write a guarded row against a real account — ever.

    Registered on the ``Session`` class itself so it holds for sessions built
    from hand-rolled engines too. Deliberately no marker/env-var escape hatch.
    """
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    event.listen(Session, "before_flush", _guard_flush)
    try:
        yield
    finally:
        event.remove(Session, "before_flush", _guard_flush)


# --------------------------------------------------------------------------
# Layer 2: the Gmail network backstop (writes blocked, reads real)
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_real_gmail_write(monkeypatch):
    try:
        from googleapiclient.http import HttpRequest
    except ImportError:  # library not installed in a stripped env
        yield
        return

    original = HttpRequest.execute

    def guarded_execute(self, *args, **kwargs):
        assert_not_real_gmail_write(
            getattr(self, "method", "GET"), getattr(self, "uri", "")
        )
        return original(self, *args, **kwargs)

    monkeypatch.setattr(HttpRequest, "execute", guarded_execute)
    yield


# --------------------------------------------------------------------------
# Real-key discipline
# --------------------------------------------------------------------------


@pytest.fixture
def _require_llm_key():
    """Real-LLM tests fail loudly — never silently skip — without the key."""
    if not os.environ.get("AGENT_NVIDIA_API_KEY"):
        # settings may load .env; check there too before failing
        try:
            from config.settings import get_settings

            s = get_settings()
            if getattr(s, "nvidia_api_key", None) or getattr(s, "has_nvidia_key", False):
                return
        except Exception:
            pass
        pytest.fail(
            "AGENT_NVIDIA_API_KEY is not set (env/.env) — real-key tests cannot run."
        )


# --------------------------------------------------------------------------
# Seeded users + API clients
# --------------------------------------------------------------------------

DEFAULT_CATEGORY_SEED = [
    # (name, rule, is_needs_review) — spec/data.md seed defaults
    ("Finance", "label_only", False),
    ("Newsletters", "label_and_archive", False),
    ("Notifications", "label_only", False),
    ("Personal", "label_only", False),
    ("Shopping", "label_only", False),
    ("Travel", "label_only", False),
    ("Needs review", "label_only", True),
]


def seed_user(session, user_id: str = "test-user-alice", email: str = "alice@example.com"):
    """Create a synthetic user + connected gmail_account + default taxonomy.

    Returns ``(user, categories_by_name)``. All ids carry the reserved
    ``test-`` prefix; the mailbox is non-routable.
    """
    from db import models

    user = models.User(id=user_id, email=email, name=f"Test {user_id}")
    session.add(user)
    account = models.GmailAccount(
        id=f"test-gma-{user_id}",
        user_id=user_id,
        google_email=email,
        refresh_token_encrypted="test-encrypted-token",
        status="connected",
    )
    session.add(account)
    categories = {}
    for position, (name, rule, is_nr) in enumerate(DEFAULT_CATEGORY_SEED):
        cat = models.Category(
            id=f"test-cat-{user_id}-{position}",
            user_id=user_id,
            name=name,
            description=f"{name} mail",
            rule=rule,
            is_needs_review=is_nr,
            position=position,
        )
        session.add(cat)
        categories[name] = cat
    session.commit()
    return user, categories


@pytest.fixture
def seeded_user(db_session):
    """``(user, categories_by_name)`` for test-user-alice with default taxonomy."""
    return seed_user(db_session)


def session_cookie_for(user_id: str) -> tuple[str, str]:
    """CROSS-SLICE CONTRACT: mint the signed session cookie for a user.

    ``src/api/session.py`` must expose a callable that signs a session for a
    user id (any of the names probed below). Fails loudly if the contract is
    not met — never a silent pass.
    """
    import api.session as session_api

    name = getattr(session_api, "SESSION_COOKIE_NAME", None) or getattr(
        session_api, "COOKIE_NAME", "zi_session"
    )
    for fn_name in (
        "issue_session_cookie",
        "create_session_cookie",
        "sign_session",
        "encode_session",
        "make_session_cookie",
    ):
        fn = getattr(session_api, fn_name, None)
        if callable(fn):
            return name, fn(user_id)
    pytest.fail(
        "session-cookie contract: api/session.py must expose "
        "issue_session_cookie(user_id) -> str (or an equivalent probed name) "
        "so tests can authenticate a seeded user."
    )


@pytest.fixture
def llm_payload_capture(monkeypatch):
    """Capture every outgoing LLM HTTP request body (the privacy tripwire).

    Both the NVIDIA (OpenAI-compatible) and Gemini clients ride on httpx, so
    wrapping ``httpx.Client.send`` / ``AsyncClient.send`` sees every payload
    that leaves the process. Tests assert the body sentinel never appears —
    and that the capture is non-empty, so a cut seam cannot pass silently.
    """
    import httpx

    captured: list[str] = []

    def _record(request: httpx.Request) -> None:
        try:
            captured.append(request.content.decode("utf-8", errors="replace"))
        except Exception:
            captured.append(repr(request.content))

    original_send = httpx.Client.send
    original_async_send = httpx.AsyncClient.send

    def send(self, request, *args, **kwargs):
        _record(request)
        return original_send(self, request, *args, **kwargs)

    async def async_send(self, request, *args, **kwargs):
        _record(request)
        return await original_async_send(self, request, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", send)
    monkeypatch.setattr(httpx.AsyncClient, "send", async_send)
    return captured


@pytest.fixture
def api_client(_isolated_db):
    """Unauthenticated FastAPI test client on the isolated DB."""
    from fastapi.testclient import TestClient

    from api.app import app

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def auth_client(api_client, seeded_user):
    """Test client authenticated as the seeded test-user-alice."""
    user, _ = seeded_user
    name, value = session_cookie_for(user.id)
    # Domain-scoped like a browser would store it, so a Set-Cookie deletion
    # (logout) actually removes it from the client jar. http.cookiejar maps the
    # dotless TestClient host "testserver" to "testserver.local" internally —
    # a domainless set() is never evicted, and "testserver" is never sent.
    api_client.cookies.set(name, value, domain="testserver.local")
    return api_client
