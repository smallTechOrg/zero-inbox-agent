import pytest

from tests.isolation import (
    GUARDED_TABLES,
    assert_isolated_db,
    assert_not_real_gmail_request,
    assert_row_not_real,
)


@pytest.fixture(autouse=True)
def _reset_settings_singleton():
    import config.settings as m
    m._settings = None
    yield
    m._settings = None


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from db.models import Base
    import db.session as session_module

    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    # Structural guard: prove the binding is a throwaway file before a single
    # row is written. A real-DB URL here is how 'e2e-actions-test' reached the
    # live account. See tests/isolation.py.
    assert_isolated_db(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(session_module, "_engine", engine)
    monkeypatch.setattr(session_module, "_SessionLocal", factory)
    monkeypatch.setattr(session_module, "init_db", lambda: None)
    yield engine
    engine.dispose()


def _guard_flush(session, _flush_context, _instances):
    """Refuse to write a guarded row against a non-test account.

    Fires on flush — i.e. on the write, inside ``session.commit()`` — never at
    teardown, so the failure lands on the test that caused it with the row in
    the message. Watches ``categories`` / ``decisions`` / ``rules`` /
    ``channel_accounts``: the four user-visible tables whose leakage the user
    would actually see in the product.

    Two independent refusals, neither of which has an opt-out:

    * the session must be bound to a throwaway ``tmp_path`` database — the
      escape that actually happened, where a cached ``get_settings()`` handed a
      script the real ``zero_inbox.db``;
    * the row must not name a real account — the ``6b4ab0f4…`` deny-list, the
      user's real mailbox in any Gmail spelling, or a bare uuid4 id (the shape
      ``db.models._uuid`` mints for real users, so a pasted production id is
      refused even though the deny-list has never seen it).
    """
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
    """Non-negotiable, suite-wide: no test may write to a real account.

    Registered on the ``Session`` class itself rather than on one factory, so it
    holds for every session any code under test opens — including one built
    from a hand-rolled engine, which is exactly how the production DB was
    polluted before. There is deliberately no marker and no env var that turns
    this off; a test that needs a real account is a spec question.
    """
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    event.listen(Session, "before_flush", _guard_flush)
    try:
        yield
    finally:
        event.remove(Session, "before_flush", _guard_flush)


@pytest.fixture(autouse=True)
def _no_real_gmail(monkeypatch):
    """The Gmail-side twin of ``_no_real_account``: no live mutation from a test.

    ``GmailMutator._execute`` is wrapped so a request object built by the real
    ``googleapiclient`` transport raises ``RealGmailError`` instead of being
    sent. A fake service passes straight through, so the existing suite is
    unaffected — but a mutator that was accidentally handed live credentials
    stops before it touches the user's actual mail, which no test teardown could
    undo.
    """
    from channels.gmail.mutations import GmailMutator

    original = GmailMutator._execute

    def _guarded_execute(self, request):
        assert_not_real_gmail_request(request)
        return original(self, request)

    monkeypatch.setattr(GmailMutator, "_execute", _guarded_execute)
    yield


@pytest.fixture
def _require_llm_key():
    """Fail loudly if the NVIDIA NIM key is missing — real-key tests never silently skip."""
    from config.settings import get_settings
    if not get_settings().has_nvidia_key:
        pytest.fail("AGENT_NVIDIA_API_KEY is not set in .env — real-key tests cannot run.")


@pytest.fixture
def api_client(_isolated_db):
    """FastAPI test client with isolated DB."""
    from fastapi.testclient import TestClient
    from api import app
    with TestClient(app) as client:
        yield client
