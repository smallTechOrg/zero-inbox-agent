"""Daily scheduler: runs incremental triage for every active connection at a
configurable UTC hour (default 6 am, controlled by ``AGENT_DIGEST_CRON_HOUR``).

Uses Python's standard ``threading`` module — no external scheduler dependency
needed. The scheduler is started/stopped in the FastAPI ``lifespan`` context in
``api/__init__.py``.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone


def _cron_hour() -> int:
    try:
        return int(os.environ.get("AGENT_DIGEST_CRON_HOUR", "6"))
    except ValueError:
        return 6


def _seconds_until_next_run(hour: int) -> float:
    """Return seconds until the next occurrence of ``hour:00 UTC``."""
    now = datetime.now(timezone.utc)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        # Already past today's run time — schedule for tomorrow
        from datetime import timedelta

        target += timedelta(days=1)
    return (target - now).total_seconds()


def _run_daily_triage() -> None:
    """Trigger incremental triage for every active channel_account."""
    import structlog

    log = structlog.get_logger("scheduler")
    log.info("daily_triage_started")

    try:
        from db.models import ChannelAccount
        from db.session import create_db_session
        from graph.runner import run_triage
        from sqlalchemy import select

        with create_db_session() as session:
            accounts = session.execute(
                select(ChannelAccount).where(ChannelAccount.status == "active")
            ).scalars().all()
            account_pairs = [(a.user_id, a.id) for a in accounts]

        for user_id, connection_id in account_pairs:
            try:
                run_triage(
                    user_id=user_id,
                    channel_account_id=connection_id,
                    limit=200,
                    dry_run=False,
                    fetch_after=None,
                )
                log.info("daily_triage_run_ok", user_id=user_id, connection_id=connection_id)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "daily_triage_run_failed",
                    user_id=user_id,
                    connection_id=connection_id,
                    error=str(exc),
                )
    except Exception as exc:  # noqa: BLE001
        log.error("daily_triage_failed", error=str(exc))


class DailyScheduler:
    """A lightweight recurring timer that fires once per day at a fixed UTC hour."""

    def __init__(self) -> None:
        self._timer: threading.Timer | None = None
        self._stopped = False

    def start(self) -> None:
        self._stopped = False
        self._schedule_next()

    def stop(self) -> None:
        self._stopped = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _schedule_next(self) -> None:
        if self._stopped:
            return
        delay = _seconds_until_next_run(_cron_hour())
        self._timer = threading.Timer(delay, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def _fire(self) -> None:
        if self._stopped:
            return
        _run_daily_triage()
        self._schedule_next()


# Module-level singleton — started/stopped by the lifespan context
_scheduler = DailyScheduler()


def start_scheduler() -> None:
    _scheduler.start()


def stop_scheduler() -> None:
    _scheduler.stop()
