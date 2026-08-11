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


@asynccontextmanager
async def _lifespan(app: FastAPI):
    from db.session import init_db

    init_db()

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
