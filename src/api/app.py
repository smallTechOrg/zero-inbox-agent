"""FastAPI application factory (spec/architecture.md, spec/api.md).

Wires the ``{ok, data|error}`` envelope handlers, ``/api/health``, structured
JSON request logging, every Phase-1 router, and the built frontend static
export. Started by ``uv run python -m src`` (uvicorn target ``api.app:app``).
"""

import json
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from api._common import VALIDATION_ERROR, error_body, ok

VERSION = "0.1.0"


def _reconcile_orphaned_runs() -> int:
    """Mark runs left ``running`` by a dead process as ``interrupted``.

    Triage runs are in-process background tasks, so a crash/restart kills them
    while their row still says ``running`` — which would both lie in the UI and
    409-block the next `POST /api/runs`. Resumability is data-driven
    (spec/agent.md): the next run's `load_chunk` skips every thread already in
    `thread_decisions`, so nothing is redone.
    """
    from db.models import Run
    from db.session import create_db_session

    closed = 0
    try:
        with create_db_session() as session:
            orphans = session.query(Run).filter(Run.status == "running").all()
            for run in orphans:
                run.status = "interrupted"
                run.interrupt_reason = (
                    "The server restarted while this run was in flight. Every decision "
                    "already made is saved — press Clean my inbox to resume where it stopped."
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
        _log_json(event="runs.orphaned_interrupted", count=closed)
    yield


def _log_json(**fields) -> None:
    """One structured JSON log line to stdout (always on; LangSmith is additive)."""
    record = {"ts": datetime.now(timezone.utc).isoformat(), **fields}
    print(json.dumps(record, default=str), flush=True)


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=error_body(code, message))


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

    @app.exception_handler(Exception)
    async def _unhandled_exc(request: Request, exc: Exception):
        # Product principle 7: no tracebacks ever reach the browser.
        _log_json(event="unhandled_error", path=str(request.url.path), error=repr(exc))
        return _error_response(
            500, "internal_error", "Something went wrong on our side — try again."
        )


def create_app() -> FastAPI:
    from config.settings import require_secret_key

    require_secret_key()  # fail fast, readably (FatalConfigError, exit code 78)

    app = FastAPI(title="Zero Inbox", version=VERSION, lifespan=_lifespan)
    _install_handlers(app)

    @app.middleware("http")
    async def _request_log(request: Request, call_next):
        """Structured JSON log per request: path, status, latency (no bodies)."""
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            _log_json(
                event="request",
                method=request.method,
                path=str(request.url.path),
                status=500,
                latency_ms=round((time.perf_counter() - started) * 1000),
            )
            raise
        # SSE and static chatter would flood the log; skip the event stream.
        if not str(request.url.path).endswith("/events"):
            _log_json(
                event="request",
                method=request.method,
                path=str(request.url.path),
                status=response.status_code,
                latency_ms=round((time.perf_counter() - started) * 1000),
            )
        return response

    @app.get("/api/health")
    async def health() -> dict:
        """Liveness + config sanity — presence booleans only, never a secret.

        Deliberately ``async`` and DB-free so it answers even when the sync
        threadpool is saturated by a long run.
        """
        from config.settings import get_settings

        s = get_settings()
        return ok(
            {
                "status": "ok",
                "version": VERSION,
                "nvidia_key_present": s.has_nvidia_key,
                "gemini_key_present": s.has_gemini_key,
                "google_oauth_configured": s.has_google_oauth,
            }
        )

    # Phase-1 routers (spec/roadmap.md slices). Standard imports — these modules
    # are contracts owned by parallel slices and exist at gate time.
    from api import audit, auth, events, runs, session, taxonomy

    app.include_router(auth.router)      # /auth/google/*, /api/auth/logout, /api/gmail/disconnect
    app.include_router(session.router)   # /api/me
    app.include_router(taxonomy.router)  # /api/taxonomy*
    app.include_router(audit.router)     # /api/audit*
    app.include_router(runs.router)      # /api/runs*
    app.include_router(events.router)    # /api/runs/{id}/events (SSE)

    # Serve the built Next.js static export (pnpm --dir frontend build → frontend/out).
    # In dev the Next server on :3000 proxies /api here; the mount is for
    # single-process serving. The API boots fine without the export.
    frontend_out = Path(__file__).resolve().parent.parent.parent / "frontend" / "out"
    if frontend_out.exists():
        app.mount("/", StaticFiles(directory=str(frontend_out), html=True), name="frontend")

    return app


app = create_app()
