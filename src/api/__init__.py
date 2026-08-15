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

    Phase 6: a run that already persisted at least one decision is marked
    **``resumable``**, not ``failed`` — every decided thread is durable and
    ``POST /api/runs/{id}/resume`` continues it without re-classifying any of them.
    Only a run with nothing persisted is ``failed``; there is nothing to resume.

    Triage runs execute as in-process background tasks, so a server restart (or
    a crash) kills them silently while their row still says ``running``. The UI
    polls that row and renders a live progress bar for work that stopped long
    ago — showing activity that isn't happening, which is worse than showing
    nothing. Any run still marked ``running`` at startup is by definition
    orphaned: this process has just booted and owns no background work yet.
    """
    from datetime import datetime, timezone

    from sqlalchemy import func, select

    from db.models import Decision, TriageRun
    from db.session import create_db_session

    closed = 0
    try:
        with create_db_session() as session:
            orphans = (
                session.query(TriageRun)
                .filter(TriageRun.status.in_(("running", "applying")))
                .all()
            )
            for run in orphans:
                decided = int(
                    session.execute(
                        select(func.count(Decision.id)).where(Decision.run_id == run.id)
                    ).scalar_one()
                    or 0
                )
                if decided > 0:
                    run.status = "resumable"
                    run.error_message = (
                        f"Interrupted at {decided} of {run.items_total or decided} "
                        "threads — nothing was left half-applied. Resume to continue."
                    )
                    # `resumable` is not terminal — no finished_at stamp.
                else:
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


def _require_secret_key() -> None:
    """Phase 8: ``AGENT_SECRET_KEY`` is required — fail at startup, not silently.

    The old ``"insecure-dev-key"`` fallback meant a deployment missing the key
    signed every session cookie with a string published in this repository, so
    anyone could mint a cookie for any user id. Refusing to start is the only
    honest behaviour.
    """
    from config.settings import require_secret_key

    # Delegated rather than re-implemented: the canonical message names the variable
    # *and* gives the command that generates a value, and the FatalConfigError it
    # raises carries exit_code 78 so the supervisor stops instead of respinning.
    require_secret_key()


def create_app() -> FastAPI:
    _require_secret_key()
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
    async def health() -> dict:
        """Liveness. Deliberately ``async`` and DB-free.

        A sync `def` endpoint runs in Starlette's shared 40-token anyio
        threadpool. During a long triage run the 1s-poll endpoints can drain
        every token, and a sync /health would then hang too — making a busy
        server indistinguishable from a dead one. Liveness must never depend on
        that pool.
        """
        return ok({"status": "ok", "version": VERSION})

    from api import (
        account,
        actions,
        categories,
        connections,
        digest,
        events,
        memory,
        runs,
        session,
        triage,
    )

    app.include_router(session.router)
    app.include_router(account.router)
    app.include_router(connections.router)
    app.include_router(runs.router)
    app.include_router(triage.router)
    app.include_router(memory.router)
    app.include_router(categories.router)
    app.include_router(actions.router)
    app.include_router(events.router)
    app.include_router(digest.router)

    # Phase 9 — inbox-derived taxonomy (slice 4):
    #   POST   /api/taxonomy/discover, POST /api/taxonomy/apply
    #   DELETE /api/categories/{id}          (refused while anything references it)
    #   GET    /api/categories/{id}/usage    (the verification step before any delete)
    from api import categories_usage, taxonomy_discovery

    app.include_router(taxonomy_discovery.router)
    app.include_router(categories_usage.router)

    # Phase 9 — the re-organisation job (slice 5). That slice EXPORTS the router
    # and deliberately does not mount it: this file has exactly one owner, so the
    # eight concurrent slices cannot collide here. Mounted defensively for the
    # window in which slice 5 has not yet landed — an unmounted route is visible
    # and loud, whereas an ImportError at startup takes the whole server down and
    # would block every other slice's gate.
    try:
        from api import reorganise

        app.include_router(reorganise.router)
    except ImportError:  # pragma: no cover - only before the reorganisation slice lands
        import sys

        print(
            "WARNING: api.reorganise not found — /api/reorg/* routes are NOT mounted",
            file=sys.stderr,
        )

    # GET /api/provider-health (Phase 6, slice 3). Mounted defensively so the app
    # still boots while that slice is in flight.
    try:
        from api import provider_health

        app.include_router(provider_health.router)
    except ImportError:  # pragma: no cover - only before the resilience slice lands
        import sys

        print(
            "WARNING: api.provider_health not found — /api/provider-health is NOT mounted",
            file=sys.stderr,
        )

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
