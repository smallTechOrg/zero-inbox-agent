"""SQLite must run in WAL with a busy timeout, and /health must never touch the DB.

Without WAL, SQLite's single writer blocks every reader outright. During a long
triage run (continuous writes) the 1s-poll endpoints — all sync `def`, so all in
Starlette's shared 40-token threadpool — stack up behind the write lock until the
pool is drained and *every* sync endpoint, /health included, stops answering.
"""

from __future__ import annotations

from sqlalchemy import text


def _engine_for(url: str):
    import db.session as session_module

    engine = None
    try:
        from sqlalchemy import create_engine

        engine = create_engine(url)
        session_module._apply_sqlite_pragmas(engine)
        return engine
    except Exception:  # pragma: no cover
        if engine is not None:
            engine.dispose()
        raise


def test_file_backed_sqlite_connections_are_wal_with_a_busy_timeout(tmp_path):
    import db.session as session_module

    engine = _engine_for(f"sqlite:///{tmp_path}/pragmas.db")
    try:
        with engine.connect() as conn:
            journal_mode = conn.execute(text("PRAGMA journal_mode")).scalar()
            busy_timeout = conn.execute(text("PRAGMA busy_timeout")).scalar()
    finally:
        engine.dispose()

    assert str(journal_mode).lower() == "wal"
    assert int(busy_timeout) == session_module.SQLITE_BUSY_TIMEOUT_MS


def test_pragmas_apply_to_every_new_connection(tmp_path):
    """The listener is on `connect`, so a second pooled connection is configured too."""
    engine = _engine_for(f"sqlite:///{tmp_path}/pragmas2.db")
    try:
        modes = []
        for _ in range(3):
            with engine.connect() as conn:
                modes.append(str(conn.execute(text("PRAGMA journal_mode")).scalar()).lower())
    finally:
        engine.dispose()
    assert modes == ["wal", "wal", "wal"]


def test_get_engine_installs_the_pragmas_for_a_sqlite_url(tmp_path, monkeypatch):
    import db.session as session_module

    monkeypatch.setattr(session_module, "_engine", None)
    monkeypatch.setattr(session_module, "_SessionLocal", None)

    class _Settings:
        database_url = f"sqlite:///{tmp_path}/via_get_engine.db"

    import config.settings as settings_module

    monkeypatch.setattr(settings_module, "get_settings", lambda: _Settings())

    engine = session_module._get_engine()
    try:
        with engine.connect() as conn:
            assert str(conn.execute(text("PRAGMA journal_mode")).scalar()).lower() == "wal"
    finally:
        engine.dispose()
        session_module._engine = None


def test_health_endpoint_is_async_so_liveness_never_needs_the_threadpool():
    """A sync /health would hang exactly when the threadpool is exhausted."""
    import inspect

    from api import app

    route = next(r for r in app.routes if getattr(r, "path", None) == "/health")
    assert inspect.iscoroutinefunction(route.endpoint), (
        "/health must be `async def` — a sync endpoint runs in the shared "
        "anyio threadpool that a busy run can drain"
    )


def test_health_still_answers(api_client):
    response = api_client.get("/health")
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "ok"
