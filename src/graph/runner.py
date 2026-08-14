"""Public entry point for a triage run."""

from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

import structlog

from graph.agent import MAX_CONCURRENCY, triage_graph
from graph.persistence import model_for, update_run
from graph.state import TriageState
from observability.events import get_logger

log = get_logger("triage")


def _ensure_run(
    *, run_id: str | None, user_id: str, channel_account_id: str, dry_run: bool, kind: str
) -> str:
    from db.session import create_db_session

    run_cls = model_for("triage_runs")
    if run_cls is None:
        return run_id or str(uuid4())

    with create_db_session() as session:
        if run_id:
            existing = session.get(run_cls, run_id)
            if existing is not None:
                existing.status = "running"
                return run_id
        new_id = run_id or str(uuid4())
        row = run_cls()
        for key, value in {
            "id": new_id,
            "user_id": user_id,
            "channel_account_id": channel_account_id,
            "kind": kind,
            "status": "running",
            "dry_run": dry_run,
            "items_total": 0,
            "items_decided": 0,
            "started_at": datetime.now(timezone.utc),
        }.items():
            if hasattr(row, key):
                setattr(row, key, value)
        session.add(row)
        session.flush()
        return row.id


def _already_decided(run_id: str) -> dict:
    """``{"ids": set[str], "count": int}`` for a (possibly resumed) run."""
    try:
        from sqlalchemy import func, select

        from db.session import create_db_session
        from graph.persistence import already_decided_item_ids

        decision_cls = model_for("decisions")
        with create_db_session() as session:
            ids = already_decided_item_ids(session, run_id)
            count = 0
            if decision_cls is not None:
                count = int(
                    session.execute(
                        select(func.count(decision_cls.id)).where(
                            decision_cls.run_id == run_id
                        )
                    ).scalar_one()
                    or 0
                )
        if count:
            log.info("triage.resuming", run_id=run_id, already_decided=count)
        return {"ids": ids, "count": count}
    except Exception as exc:  # pragma: no cover - a fresh run must never fail here
        log.warning("triage.resume_load_failed", run_id=run_id, error=str(exc))
        return {"ids": set(), "count": 0}


def _reset_provider_health(run_id: str) -> None:
    """Fresh circuit + chain position 0 on every start/resume (Rules E + F)."""
    try:
        from llm import health  # type: ignore[attr-defined]
    except ImportError:  # pragma: no cover - slice 3 not landed yet
        return
    reset = getattr(health, "reset", None)
    if reset is None:  # pragma: no cover
        return
    try:
        reset(run_id)
    except Exception as exc:  # pragma: no cover - never fail a run over telemetry
        log.warning("triage.health_reset_failed", run_id=run_id, error=str(exc))


@contextmanager
def _bind_provider_health(run_id: str, user_id: str):
    """`llm.health.bind_run(...)` if slice 3 is present, else a no-op.

    Defensive so the runner keeps working if the provider-resilience module is
    absent, matching `_reset_provider_health` above.
    """
    try:
        from llm import health  # type: ignore[attr-defined]

        binder = getattr(health, "bind_run", None)
    except ImportError:  # pragma: no cover - slice 3 not landed
        binder = None
    if binder is None:  # pragma: no cover
        yield
        return
    with binder(run_id, user_id):
        yield


def execute_triage(
    *,
    user_id: str,
    channel_account_id: str,
    limit: int = 10_000,
    dry_run: bool = True,
    run_id: str | None = None,
    items: list[dict] | None = None,
    kind: str = "incremental",
    fetch_after: str | None = None,
) -> TriageState:
    """Run the cascade and return the final graph state (run_id included).

    ``items`` lets a caller supply already-ingested threads instead of having
    ``fetch_items`` pull them from the channel adapter.

    ``fetch_after`` (ISO timestamp, optional) restricts the fetch to threads
    newer than that cutoff — an incremental "what's new" run.
    """
    resolved_run_id = _ensure_run(
        run_id=run_id,
        user_id=user_id,
        channel_account_id=channel_account_id,
        dry_run=dry_run,
        kind=kind,
    )

    # Bind once here so EVERY log line emitted anywhere under this run — graph
    # nodes, the Gmail adapter, LLM retries, third-party code using structlog —
    # carries user_id/run_id and is therefore routed to that user's activity
    # stream by activity_bus_processor. Without this the UI only sees the
    # hand-placed bus.emit() calls, which is how Gmail 429 backoffs and LLM
    # retries stayed invisible.
    structlog.contextvars.bind_contextvars(
        user_id=user_id, run_id=resolved_run_id
    )

    # Resume: everything this run already decided is loaded up-front and seeded into
    # state, so fetch_items can drop those threads from every downstream queue. A
    # fresh run simply finds nothing (spec/capabilities/durable-resumable-runs.md B).
    already_decided = _already_decided(resolved_run_id)
    _reset_provider_health(resolved_run_id)

    initial: TriageState = {
        "run_id": resolved_run_id,
        "user_id": user_id,
        "channel_account_id": channel_account_id,
        "limit": limit,
        "dry_run": dry_run,
        "fetch_after": fetch_after,
        "status": "running",
        "error": None,
        "resolved": [],
        "llm_decisions": [],
        "deep_queue": [],
        "llm_calls": [],
        "already_decided_item_ids": sorted(already_decided["ids"]),
        "already_decided_count": already_decided["count"],
        "review_failed_item_ids": [],
    }
    if items is not None:
        initial["items"] = items

    started = time.time()
    try:
        # Bind the run to llm.health for the WHOLE invocation, so every provider
        # counter, circuit-breaker decision, model rotation and degraded warning
        # is attributed to this run and this user — and therefore reaches the
        # user's activity feed instead of being dropped as unattributed. Without
        # this the binding was dead code: defined, exported, never called.
        # (Graph nodes hop to a worker thread for their async LLM calls;
        # `_run_async` copies the context across so this binding survives.)
        with _bind_provider_health(resolved_run_id, user_id):
            final = triage_graph.invoke(
                initial,
                config={"max_concurrency": MAX_CONCURRENCY, "recursion_limit": 100},
            )
    except Exception as exc:
        log.error("triage.crashed", run_id=resolved_run_id, error=str(exc))
        from db.session import create_db_session

        with create_db_session() as session:
            update_run(session, run_id=resolved_run_id, status="failed", error=str(exc))
        raise

    log.info(
        "triage.run_finished",
        run_id=resolved_run_id,
        status=final.get("status"),
        duration_ms=int((time.time() - started) * 1000),
    )
    return final


def run_triage(
    *,
    user_id: str,
    channel_account_id: str,
    limit: int = 10_000,
    dry_run: bool = True,
    run_id: str | None = None,
    items: list[dict] | None = None,
    fetch_after: str | None = None,
    triggered_by: str = "user",
) -> str:
    """Creates (or resumes) a TriageRun, invokes the triage graph, returns ``run_id``."""
    from events import bus

    # We need to resolve the run_id before emitting so the event carries it.
    resolved_id = _ensure_run(
        run_id=run_id,
        user_id=user_id,
        channel_account_id=channel_account_id,
        dry_run=dry_run,
        kind="incremental",
    )
    bus.emit(
        user_id,
        {
            "type": "run_started",
            "ts": time.time(),
            "run_id": resolved_id,
            "connection_id": channel_account_id,
            "triggered_by": triggered_by,
        },
    )

    final = execute_triage(
        user_id=user_id,
        channel_account_id=channel_account_id,
        limit=limit,
        dry_run=dry_run,
        run_id=resolved_id,
        items=items,
        fetch_after=fetch_after,
    )
    return final["run_id"]
