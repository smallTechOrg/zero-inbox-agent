"""Activity watchdog — *not one beat without a log being published*.

A tier-3 batch is a single LLM call over ~29 threads: between dispatch and
return the graph publishes **nothing**. On a degraded provider that was minutes
of dead air, which is exactly how a working run became indistinguishable from a
hung one. Working-but-slow must never look identical to stuck.

This module guarantees, by construction, that while a run is active no gap
longer than :data:`HEARTBEAT_INTERVAL_SECONDS` passes with nothing published.

Design notes (spec/capabilities/triage-transparency.md, rules I3–I7):

* **Self-arming.** No graph code starts or stops it. The only caller is
  :func:`observability.logging.activity_bus_processor`, which sees *every*
  structlog line for a bound user. Any event carrying a ``run_id`` arms the
  watchdog; ``run_completed`` / ``run_resumable`` / ``run_failed`` / ``error``
  disarms it. A lifecycle nobody has to remember cannot drift.
* **Real state, not a spinner.** The heartbeat replays the last *observed*
  phase, batch index, batch size and model taken from the most recent log line
  for that run, plus how long it has been in that phase. A content-free tick is
  the same as silence.
* **Never noisy.** It only fires when nothing has been published for the whole
  interval, so a graph publishing faster than 3 s produces zero heartbeats.
* **Counts, ids, phases and elapsed times only** — never a subject, sender or
  body.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

#: Comfortably below the ~5 s at which a human reads a static screen as "stuck".
HEARTBEAT_INTERVAL_SECONDS: float = 3.0

#: How often the single daemon thread wakes to look for silent runs.
SWEEP_INTERVAL_SECONDS: float = 0.5

#: Leak guard: a run that publishes nothing real for this long is abandoned.
IDLE_CEILING_SECONDS: float = 600.0

#: Event names (bare or ``triage.``-prefixed) that end a run's activity.
DISARM_EVENTS: frozenset[str] = frozenset(
    {"run_completed", "run_resumable", "run_failed", "error"}
)

_HEARTBEAT_TYPE = "activity_heartbeat"


@dataclass
class _RunActivity:
    """Last observed state of one ``(user_id, run_id)`` pair."""

    user_id: str
    run_id: str
    phase: str = "starting"
    detail: str = ""
    batch_n: int | None = None
    batch_total: int | None = None
    batch_size: int | None = None
    model: str | None = None
    #: monotonic time of the last publication of any kind (log line OR heartbeat)
    last_publication: float = field(default_factory=time.monotonic)
    #: monotonic time of the last *real* activity (log line only) — idle guard
    last_activity: float = field(default_factory=time.monotonic)
    #: monotonic time the current phase began — the source of ``elapsed_s``
    phase_started: float = field(default_factory=time.monotonic)


_lock = threading.RLock()
_runs: dict[tuple[str, str], _RunActivity] = {}
_thread: threading.Thread | None = None
_stop = threading.Event()


# ── phase derivation ─────────────────────────────────────────────────────────


def _normalise(event: str | None) -> str:
    name = str(event or "").strip()
    return name.split(".", 1)[1] if name.startswith("triage.") else name


def _derive_phase(event: str | None, fields: dict[str, Any]) -> str | None:
    """Map an observed log-event name onto a user-meaningful phase name.

    Returns ``None`` when the event says nothing about the phase (e.g. a
    generic operation log), in which case the previous phase is kept.
    """
    name = _normalise(event)
    tier = fields.get("tier")
    if name in ("batch_dispatched", "batch_returned", "tier_started", "tier_finished"):
        return f"tier{tier}_classify" if tier not in (None, "") else "classify"
    if name == "page_fetched":
        return "fetching_inbox"
    if name in ("reviewer_started", "reviewer_finished"):
        return "second_pass_review"
    if name == "checkpoint":
        return "checkpoint"
    if name == "apply_progress":
        return "applying"
    if name.startswith("llm."):
        return None  # LLM chatter refines nothing; the graph owns the phase
    if name in ("call_started", "call_finished", "retry", "model_fallback"):
        return None
    return None


def _describe(state: _RunActivity) -> str:
    if state.batch_n and state.batch_total:
        threads = f" — {state.batch_size} threads" if state.batch_size else ""
        return f"batch {state.batch_n}/{state.batch_total}{threads}"
    return state.phase.replace("_", " ")


# ── public API ───────────────────────────────────────────────────────────────


def note_activity(
    user_id: str,
    *,
    run_id: str | None,
    phase: str | None,
    fields: dict[str, Any] | None = None,
) -> None:
    """Record "this run just published something", and what it was publishing.

    Called ONLY from ``observability.logging.activity_bus_processor``. Never
    raises — telemetry must never break logging.
    """
    try:
        if not user_id or not run_id:
            return  # cannot arm a watchdog for an unattributable event
        fields = dict(fields or {})
        key = (str(user_id), str(run_id))

        name = str(phase or "")
        if _normalise(phase) in DISARM_EVENTS or name.rsplit(".", 1)[-1] in DISARM_EVENTS:
            with _lock:
                _runs.pop(key, None)
            return

        now = time.monotonic()
        with _lock:
            state = _runs.get(key)
            if state is None:
                state = _RunActivity(user_id=str(user_id), run_id=str(run_id))
                _runs[key] = state
            state.last_publication = now
            state.last_activity = now

            derived = _derive_phase(phase, fields)
            if derived and derived != state.phase:
                state.phase = derived
                state.phase_started = now
                # A new phase invalidates the previous batch coordinates.
                if not _normalise(phase).startswith("batch"):
                    state.batch_n = state.batch_total = state.batch_size = None

            for attr in ("batch_n", "batch_total", "batch_size"):
                value = fields.get(attr)
                if isinstance(value, int):
                    setattr(state, attr, value)
                    if attr == "batch_n":
                        # A new batch restarts the elapsed clock for the batch.
                        state.phase_started = now
            model = fields.get("model")
            if isinstance(model, str) and model:
                state.model = model
            state.detail = _describe(state)

        start_watchdog()
    except Exception:  # pragma: no cover — defensive: never break logging
        pass


def armed_runs() -> list[tuple[str, str]]:
    """The ``(user_id, run_id)`` pairs currently being watched (for tests)."""
    with _lock:
        return sorted(_runs)


def start_watchdog() -> None:
    """Start the single process-wide daemon sweeper. Idempotent."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(
            target=_sweep_forever, name="activity-heartbeat", daemon=True
        )
        _thread.start()


def stop_watchdog() -> None:
    """Stop the sweeper and forget every armed run (used by tests)."""
    global _thread
    with _lock:
        thread = _thread
        _thread = None
        _runs.clear()
    _stop.set()
    if thread is not None and thread.is_alive():
        thread.join(timeout=2 * SWEEP_INTERVAL_SECONDS + 1.0)


def _sweep_forever() -> None:
    # Bounded wait, never a bare sleep loop: stop_watchdog() returns promptly.
    while not _stop.wait(SWEEP_INTERVAL_SECONDS):
        try:
            sweep_once()
        except Exception:  # pragma: no cover — a sweep error must not kill the thread
            pass


def sweep_once(now: float | None = None) -> int:
    """Emit a heartbeat for every armed run that has gone silent. Returns count."""
    now = time.monotonic() if now is None else now
    due: list[tuple[_RunActivity, float]] = []
    with _lock:
        for key, state in list(_runs.items()):
            if now - state.last_activity >= IDLE_CEILING_SECONDS:
                _runs.pop(key, None)  # leak guard: the run is gone, not slow
                continue
            silent_for = now - state.last_publication
            if silent_for >= HEARTBEAT_INTERVAL_SECONDS:
                state.last_publication = now
                due.append((state, silent_for))
    for state, silent_for in due:
        _publish(state, silent_for, now)
    return len(due)


def _publish(state: _RunActivity, silent_for: float, now: float) -> None:
    """Emit one ``activity_heartbeat``. Counts, ids, phases, models only."""
    try:
        from events import bus

        bus.emit(
            state.user_id,
            {
                "type": _HEARTBEAT_TYPE,
                "run_id": state.run_id,
                "phase": state.phase,
                "detail": state.detail or state.phase.replace("_", " "),
                "batch_n": state.batch_n,
                "batch_total": state.batch_total,
                "batch_size": state.batch_size,
                "model": state.model,
                "elapsed_s": round(now - state.phase_started, 1),
                "silent_for_s": round(silent_for, 1),
            },
        )
    except Exception:  # pragma: no cover — the bus must never break the watchdog
        pass
