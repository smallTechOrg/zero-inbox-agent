"""FastAPI application factory.

Wires the response envelope, the exception handlers that render `api_error()` as
``{"data": null, "error": {code, message}}``, every Phase-1 router, and the Next.js
static export at ``/app``.
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from api._common import VALIDATION_ERROR, ok

VERSION = "0.1.0"


def _reconcile_orphaned_runs() -> int:
    """Close out runs left ``running`` by a process that no longer exists.

    Triage runs execute as in-process background tasks, so a server restart (or
    a crash) kills them silently while their row still says ``running``. The UI
    polls that row and renders a live progress bar for work that stopped long
    ago — showing activity that isn't happening, which is worse than showing
    nothing. Any run still marked ``running`` at startup is by definition
    orphaned: this process has just booted and owns no background work yet.
    """
    from datetime import datetime, timezone

    from db.models import TriageRun
    from db.session import create_db_session

    closed = 0
    try:
        with create_db_session() as session:
            orphans = (
                session.query(TriageRun).filter(TriageRun.status == "running").all()
            )
            for run in orphans:
                run.status = "failed"
                run.error_message = (
                    "Interrupted — the server restarted while this run was in "
                    "flight, so it stopped. Nothing was left half-applied: only "
                    "approved decisions ever mutate Gmail. Start a new run."
                )
                run.finished_at = datetime.now(timezone.utc)
                closed += 1
    except Exception:  # noqa: BLE001 — never block startup on reconciliation
        return 0
    return closed


@asynccontextmanager
async def _lifespan(app: FastAPI):
    from db.session import init_db

    init_db()

    closed = _reconcile_orphaned_runs()
    if closed:
        from observability.events import get_logger

        get_logger("startup").warning("runs.orphaned_closed", count=closed)

    try:
        from scheduler import start_scheduler, stop_scheduler

        start_scheduler()
        yield
        stop_scheduler()
    except Exception:  # noqa: BLE001 — scheduler must never block the server from starting
        yield


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"data": None, "error": {"code": code, "message": message}},
    )


def _install_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def _http_exc(request: Request, exc: HTTPException):
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            return _error_response(exc.status_code, detail["code"], detail.get("message", ""))
        return _error_response(exc.status_code, "http_error", str(detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_exc(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", []))
        message = f"{loc}: {first.get('msg', 'invalid request')}".strip(": ")
        return _error_response(422, VALIDATION_ERROR, message)


def create_app() -> FastAPI:
    app = FastAPI(title="Zero Inbox Agent", version=VERSION, lifespan=_lifespan)
    _install_handlers(app)

    @app.middleware("http")
    async def _bind_user_to_logs(request, call_next):
        """Bind the signed-in user to structlog contextvars for the request.

        Without this only triage runs carried a user_id, so everything that
        happens inside an ordinary API call — the whole archive/apply path, the
        Gmail mutations, their retries — logged with no user attached and was
        therefore dropped by activity_bus_processor instead of reaching the
        activity feed. That is precisely the work that had no UI visibility.
        """
        import structlog

        from api.session import optional_user_id

        structlog.contextvars.clear_contextvars()
        try:
            user_id = optional_user_id(request)
        except Exception:  # noqa: BLE001 — never fail a request over telemetry
            user_id = None
        if user_id:
            structlog.contextvars.bind_contextvars(user_id=user_id)
        try:
            return await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()

    @app.get("/health")
    def health() -> dict:
        return ok({"status": "ok", "version": VERSION})

    from api import actions, categories, connections, digest, events, memory, runs, session, triage

    app.include_router(session.router)
    app.include_router(connections.router)
    app.include_router(runs.router)
    app.include_router(triage.router)
    app.include_router(memory.router)
    app.include_router(categories.router)
    app.include_router(actions.router)
    app.include_router(events.router)
    app.include_router(digest.router)

    # Google OAuth routes live in api/auth.py (gmail-adapter slice). Mounted at the
    # paths spec/api.md documents: /auth/google/start, /auth/google/callback, /auth/logout.
    try:
        from api import auth

        app.include_router(auth.router)
    except ImportError:  # pragma: no cover - only before the auth slice lands
        import sys

        print("WARNING: api.auth not found — /auth/google/* routes are NOT mounted", file=sys.stderr)

    # Serve the built Next.js static export at /app.
    # Run `cd frontend && pnpm build` to generate frontend/out/ before starting.
    # Server starts fine without it (API-only mode when out/ doesn't exist).
    frontend_out = Path(__file__).resolve().parent.parent.parent / "frontend" / "out"
    if frontend_out.exists():
        app.mount("/app", StaticFiles(directory=str(frontend_out), html=True), name="frontend")

    return app


app = create_app()
