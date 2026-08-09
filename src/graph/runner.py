"""Public entry point for a triage run."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from uuid import uuid4

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


def execute_triage(
    *,
    user_id: str,
    channel_account_id: str,
    limit: int = 200,
    dry_run: bool = True,
    run_id: str | None = None,
    items: list[dict] | None = None,
    kind: str = "incremental",
) -> TriageState:
    """Run the cascade and return the final graph state (run_id included).

    ``items`` lets a caller supply already-ingested threads instead of having
    ``fetch_items`` pull them from the channel adapter.
    """
    resolved_run_id = _ensure_run(
        run_id=run_id,
        user_id=user_id,
        channel_account_id=channel_account_id,
        dry_run=dry_run,
        kind=kind,
    )

    initial: TriageState = {
        "run_id": resolved_run_id,
        "user_id": user_id,
        "channel_account_id": channel_account_id,
        "limit": limit,
        "dry_run": dry_run,
        "status": "running",
        "error": None,
        "resolved": [],
        "llm_decisions": [],
        "deep_queue": [],
        "llm_calls": [],
    }
    if items is not None:
        initial["items"] = items

    started = time.time()
    try:
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
    limit: int = 200,
    dry_run: bool = True,
    run_id: str | None = None,
    items: list[dict] | None = None,
) -> str:
    """Creates (or resumes) a TriageRun, invokes the triage graph, returns ``run_id``."""
    final = execute_triage(
        user_id=user_id,
        channel_account_id=channel_account_id,
        limit=limit,
        dry_run=dry_run,
        run_id=run_id,
        items=items,
    )
    return final["run_id"]
