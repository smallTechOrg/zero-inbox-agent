from contextlib import contextmanager
from collections.abc import Generator

from sqlalchemy import create_engine, event, Engine
from sqlalchemy.orm import Session, sessionmaker

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None

#: How long a reader/writer waits for SQLite's single write lock before giving
#: up with "database is locked". A long triage run writes continuously, and the
#: 1s-poll endpoints run in Starlette's shared 40-token threadpool: without a
#: busy timeout each blocked poll raises immediately, and with the default
#: rollback journal every reader is blocked outright for the whole write.
SQLITE_BUSY_TIMEOUT_MS = 10_000


def _apply_sqlite_pragmas(engine: Engine) -> None:
    """WAL + busy_timeout on every SQLite connection.

    WAL lets readers proceed *while* a writer holds the lock (they read the last
    committed snapshot), which is what keeps the polling endpoints answering
    during a long run instead of piling up in the threadpool. Only applied to
    SQLite; Postgres needs neither.
    """

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _record):  # pragma: no cover - via engine
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()


def _get_engine() -> Engine:
    global _engine
    if _engine is None:
        from config.settings import get_settings
        url = get_settings().database_url
        _engine = create_engine(url, echo=False)
        if _engine.dialect.name == "sqlite":
            _apply_sqlite_pragmas(_engine)
    return _engine


def _get_session_factory() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=_get_engine(), autoflush=False, autocommit=False)
    return _SessionLocal


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency."""
    with _get_session_factory()() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


@contextmanager
def create_db_session() -> Generator[Session, None, None]:
    """Standalone — for graph nodes, CLI, scripts."""
    with _get_session_factory()() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def init_db() -> None:
    from db.models import Base
    Base.metadata.create_all(bind=_get_engine())
