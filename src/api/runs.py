"""Triage run progress (polled once a second by the progress bar) and cancellation."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api._common import DRY_RUN_VIOLATION, NOT_FOUND, PROVIDER_ERROR, VALIDATION_ERROR, api_error, iso, not_found, ok
from api.session import require_user_id
from db.session import get_session
from graph.remainder import remainder_ledger

_log = logging.getLogger("zero_inbox.api.runs")

router = APIRouter()

TERMINAL_STATUSES = ("completed", "failed", "cancelled")

#: A run interrupted with work already persisted. Not terminal: it can be put back
#: to ``running`` by POST /api/runs/{id}/resume without redoing a single thread.
RESUMABLE_STATUS = "resumable"

#: Decisions are made and persisted; the Gmail apply pass is still running.
#: Non-terminal on purpose — a client must never see a terminal run whose
#: apply outcome is still in flight, because `apply_ok`/`distance_to_zero`
#: are not yet settled and the UI would render a false failure.
APPLYING_STATUS = "applying"

#: spec/api.md "Error codes" — 409, the run has no partial work to resume.
NOT_RESUMABLE = "not_resumable"

#: spec/api.md "Error codes" — 409, the run is not ``completed`` so its decisions
#: cannot be applied.
NOT_APPLIABLE = "not_appliable"

#: Run ids whose retry-apply pass is currently in flight in this process. A second
#: POST while the first is still working is a no-op rather than a second worker
#: racing the same rows (the pass is idempotent per decision, but two passes would
#: still double the Gmail calls).
_apply_in_flight: set[str] = set()
_apply_lock = threading.Lock()


def run_payload(run, session: Session) -> dict:
    items_total = run.items_total or 0
    items_decided = run.items_decided or 0
    # spec/api.md: every run surface carries how far from zero it is and whether the
    # apply pass actually worked, so no surface can render a run that archived
    # nothing as a clean success. Computed live — never from cached counts.
    ledger = remainder_ledger(session, run_id=run.id, user_id=run.user_id)
    return {
        "distance_to_zero": ledger["distance_to_zero"],
        "apply_ok": ledger["apply_ok"],
        "id": run.id,
        "status": run.status,
        "resumable": run.status == RESUMABLE_STATUS,
        "remaining": max(items_total - items_decided, 0),
        "dry_run": bool(run.dry_run),
        "items_total": run.items_total or 0,
        "items_decided": run.items_decided or 0,
        "counts": run.counts or {},
        "cost": {
            "tokens_in": run.tokens_in or 0,
            "tokens_out": run.tokens_out or 0,
            "usd": run.cost_usd or 0.0,
        },
        "error_message": run.error_message,
        "started_at": iso(run.started_at),
        "finished_at": iso(run.finished_at),
    }


def load_run(session: Session, run_id: str, user_id: str):
    from db.models import TriageRun

    run = session.get(TriageRun, run_id)
    if run is None or run.user_id != user_id:
        raise not_found("Run")
    return run


@router.get("/api/runs/latest")
def get_latest_run(
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    """The run worth showing on dashboard load — completed or running only.

    Registered ahead of ``/api/runs/{run_id}`` so the literal path ``latest``
    is matched here rather than treated as a run id (Starlette matches routes
    in registration order, not by specificity).
    """
    from db.models import TriageRun

    # ``resumable`` is included alongside completed/running: an interrupted run with
    # persisted work is exactly what the dashboard must surface (the Resume banner,
    # ui.md screen 13) — never a failed-run message.
    run_id = session.execute(
        select(TriageRun.id)
        .where(
            TriageRun.user_id == user_id,
            TriageRun.status.in_(("completed", "running", APPLYING_STATUS, RESUMABLE_STATUS)),
        )
        .order_by(TriageRun.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if run_id is None:
        return ok(None)
    return ok(run_payload(load_run(session, run_id, user_id), session))


@router.get("/api/runs/{run_id}")
def get_run(
    run_id: str,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    return ok(run_payload(load_run(session, run_id, user_id), session))


@router.get("/api/runs/{run_id}/summary")
def get_run_summary(
    run_id: str,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    from db.models import Cluster, Decision, TriageRun

    run = load_run(session, run_id, user_id)

    # Categories: group decisions by category name via join
    from db.models import Category

    cat_rows = session.execute(
        select(Category.name, Decision.proposed_action, func.count(Decision.id).label("cnt"))
        .join(Category, Decision.category_id == Category.id)
        .where(Decision.run_id == run_id, Decision.user_id == user_id)
        .group_by(Category.name, Decision.proposed_action)
    ).all()

    # Aggregate categories: pick the dominant suggested_action per name
    cat_map: dict[str, dict] = {}
    for name, action, cnt in cat_rows:
        if name not in cat_map:
            cat_map[name] = {"name": name, "count": 0, "suggested_action": action}
        cat_map[name]["count"] += cnt
        # keep highest-count action per category
        if cnt > cat_map[name]["count"]:
            cat_map[name]["suggested_action"] = action

    categories = list(cat_map.values())

    # Top 3 clusters by item_count
    top_cluster_rows = session.execute(
        select(Cluster.label, Cluster.item_count, Cluster.suggested_action)
        .where(Cluster.run_id == run_id, Cluster.user_id == user_id)
        .order_by(Cluster.item_count.desc())
        .limit(3)
    ).all()

    top_clusters = [
        {"label": label, "count": count, "suggested_action": action}
        for label, count, action in top_cluster_rows
    ]

    needs_your_call_count = session.execute(
        select(func.count(Decision.id)).where(
            Decision.run_id == run_id,
            Decision.user_id == user_id,
            Decision.status == "needs_your_call",
        )
    ).scalar_one()

    # spec/api.md Phase 7: the summary reflects what was DONE. ``applied_count`` was
    # specified in Phase 3 and never implemented; it is closed here from the live
    # ledger rather than from run.counts, so it can never drift from the decisions.
    ledger = remainder_ledger(session, run_id=run_id, user_id=user_id)

    return ok(
        {
            "run_id": run_id,
            "status": run.status,
            "total_threads": run.items_total or 0,
            "applied_count": ledger["applied"],
            "distance_to_zero": ledger["distance_to_zero"],
            "remainder": ledger["remainder"],
            "categories": categories,
            "top_clusters": top_clusters,
            "needs_your_call_count": needs_your_call_count or 0,
            "cost_usd": run.cost_usd or 0.0,
            "completed_at": iso(run.finished_at),
        }
    )


@router.get("/api/runs/{run_id}/remainder")
def get_run_remainder(
    run_id: str,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    """The honest answer to "how far from zero am I, and why?".

    Computed live from ``decisions`` on every call. ``404`` when the run does not
    exist **or belongs to another user** — a run id is not a capability.
    """
    load_run(session, run_id, user_id)  # 404s if absent or another user's
    return ok(remainder_ledger(session, run_id=run_id, user_id=user_id))


def _retry_apply_task(*, run_id: str, user_id: str, channel_account_id: str, dry_run: bool) -> None:
    """Re-run the apply pass for one completed run. Never raises into the server."""
    try:
        from graph.nodes import apply_run_decisions

        ledger = apply_run_decisions(
            run_id=run_id,
            user_id=user_id,
            channel_account_id=channel_account_id,
            dry_run=dry_run,
        )
        _log.info(
            "runs.retry_apply_complete run_id=%s applied=%s already_applied=%s failed=%s",
            run_id,
            (ledger or {}).get("applied"),
            (ledger or {}).get("already_applied"),
            (ledger or {}).get("failed"),
        )
    except Exception:
        # Loud, never silent: the ledger surfaces on the next GET /remainder, and the
        # failure is on the record here rather than swallowed by the task runner.
        _log.warning("runs.retry_apply_failed run_id=%s", run_id, exc_info=True)
    finally:
        with _apply_lock:
            _apply_in_flight.discard(run_id)


@router.post("/api/runs/{run_id}/apply")
def apply_run(
    run_id: str,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    """Re-run the apply pass for a ``completed`` run — no thread is re-classified.

    The recovery path for a transient Gmail/auth failure: it spends no tokens.
    Idempotent — already-``applied`` decisions are counted in ``already_applied``
    and never mutated twice, so calling it twice in a row is a no-op the second
    time. ``409 not_appliable`` when the run's status is not ``completed``.
    """
    run = load_run(session, run_id, user_id)  # 404s if absent or another user's
    if run.status != "completed":
        raise api_error(
            NOT_APPLIABLE,
            f"only a completed run can be applied (status={run.status!r})",
            409,
        )

    with _apply_lock:
        queued = run_id not in _apply_in_flight
        if queued:
            _apply_in_flight.add(run_id)

    if queued:
        background_tasks.add_task(
            _retry_apply_task,
            run_id=run.id,
            user_id=user_id,
            channel_account_id=run.channel_account_id,
            dry_run=bool(run.dry_run),
        )

    # The pass runs in the background; the response is the ledger as it stands right
    # now, which is what the Inbox-Zero card re-renders from. The finished ledger
    # arrives on the SSE bus (apply_progress / inbox_zero_report).
    payload = remainder_ledger(session, run_id=run_id, user_id=user_id)
    payload["queued"] = queued
    return ok(payload)


@router.post("/api/runs/{run_id}/resume")
def resume_run(
    run_id: str,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    """Restart the SAME run row, skipping every thread that already has a decision.

    No new run is created, so counts and cost stay on one row and spend is additive.
    ``409 not_resumable`` when the run has no partial work to resume. Idempotent: a
    run already back in ``running`` returns the same payload and no second background
    task is started.
    """
    from sqlalchemy import update

    from api.connections import _run_triage_task
    from db.models import TriageRun

    run = load_run(session, run_id, user_id)  # 404s if absent or another user's
    payload = {
        "run_id": run.id,
        "items_total": run.items_total or 0,
        "items_decided": run.items_decided or 0,
        "remaining": max((run.items_total or 0) - (run.items_decided or 0), 0),
    }
    if run.status == "running":
        return ok(payload)  # already resumed — never start a second worker
    if run.status != RESUMABLE_STATUS:
        raise api_error(
            NOT_RESUMABLE,
            f"this run has no partial work to resume (status={run.status!r})",
            409,
        )

    # The resumable -> running transition is a single atomic conditional UPDATE,
    # NOT the read-check-write above. Those checks are only for the error
    # responses; they cannot decide who starts the worker.
    #
    # Read-check-then-commit leaves a TOCTOU window: two concurrent resumes (a
    # double-click, or a client retry on a flaky connection) both read
    # 'resumable', both write 'running', and both schedule a background task —
    # two workers decide the same run at once, double-counting items_decided and
    # cost. qa-auditor reproduced exactly that: 5 concurrent requests spawned 2
    # workers, twice. Only the request whose UPDATE actually matches a row that
    # is STILL 'resumable' wins; the database arbitrates, not the application.
    won = session.execute(
        update(TriageRun)
        .where(
            TriageRun.id == run_id,
            TriageRun.user_id == user_id,
            TriageRun.status == RESUMABLE_STATUS,
        )
        .values(status="running", finished_at=None, error_message=None)
    ).rowcount
    session.commit()

    if not won:
        # Another concurrent request flipped it first. Same payload, no second
        # worker — identical to the already-running branch above.
        return ok(payload)

    background_tasks.add_task(
        _run_triage_task,
        run_id=run.id,
        user_id=user_id,
        channel_account_id=run.channel_account_id,
        limit=max(run.items_total or 0, 10_000),
        dry_run=bool(run.dry_run),
    )
    return ok(payload)


@router.post("/api/runs/{run_id}/undo")
def undo_run(
    run_id: str,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    """Reverse all Gmail mutations from this run in reverse chronological order.

    Idempotent: a second call on an already-undone run returns the same result.
    Only ``completed`` runs may be undone — not running or cancelled runs.
    """
    from db.models import ActionLog, Decision

    run = load_run(session, run_id, user_id)
    if run.status not in ("completed",):
        raise api_error(
            VALIDATION_ERROR,
            f"can only undo a completed run (status={run.status!r})",
            422,
        )

    # Load all action logs for this run's decisions, reverse-chronological.
    action_logs = session.execute(
        select(ActionLog)
        .join(Decision, ActionLog.decision_id == Decision.id)
        .where(
            ActionLog.user_id == user_id,
            Decision.run_id == run_id,
        )
        .order_by(ActionLog.created_at.desc())
    ).scalars().all()

    if not action_logs:
        return ok({"reversed": 0, "skipped": 0, "errors": []})

    from api.actions import _mutator_and_labels_for_user
    from channels.base import ChannelError

    try:
        mutator, _ = _mutator_and_labels_for_user(session, user_id)
    except Exception as exc:
        raise api_error(PROVIDER_ERROR, f"could not build Gmail client: {exc}", 502) from exc

    reversed_count = 0
    skipped_count = 0
    errors: list[str] = []

    for action_log in action_logs:
        if action_log.undone_at is not None:
            # Already undone — idempotent skip.
            skipped_count += 1
            continue
        try:
            token = action_log.undo_token or {}
            thread_id = token.get("thread_id")
            if not thread_id:
                errors.append(f"action_log {action_log.id}: missing thread_id in undo_token")
                continue

            original_label_ids = token.get("original_label_ids")
            labels_added = token.get("labels_added_by_triage") or [token.get("category_label_id")]
            labels_added = [lb for lb in labels_added if lb]

            if original_label_ids is not None:
                mutator.restore_labels(
                    thread_id,
                    add_label_ids=original_label_ids,
                    remove_label_ids=labels_added,
                )
            else:
                # Legacy token — fall back to implicit inverse.
                category_label_id = token.get("category_label_id")
                if not category_label_id:
                    errors.append(f"action_log {action_log.id}: undo_token lacks category_label_id")
                    continue
                mutator.undo_archive_and_label(thread_id, category_label_id=category_label_id)

            from datetime import datetime, timezone

            action_log.undone_at = datetime.now(timezone.utc)
            if action_log.decision_id:
                decision = session.get(Decision, action_log.decision_id)
                if decision is not None and decision.user_id == user_id:
                    decision.status = "undone"
            session.flush()
            reversed_count += 1
        except ChannelError as exc:
            errors.append(f"action_log {action_log.id}: {exc}")
        except Exception as exc:
            errors.append(f"action_log {action_log.id}: {exc}")

    return ok({"reversed": reversed_count, "skipped": skipped_count, "errors": errors})


@router.post("/api/runs/{run_id}/cancel")
def cancel_run(
    run_id: str,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    run = load_run(session, run_id, user_id)
    # Cancellation is a persisted flag: the graph checks the run row between batches.
    # Cancelling an already-finished run is a no-op that reports its real status.
    if run.status not in TERMINAL_STATUSES:
        run.status = "cancelled"
        run.finished_at = datetime.now(timezone.utc)
    return ok({"status": run.status})
