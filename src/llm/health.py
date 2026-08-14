"""Per-run LLM provider health: counters, circuit breaker and model fallback chain.

Three concerns live here (spec/capabilities/durable-resumable-runs.md rules E, F, H):

* **Health counters** — `calls`, `retries`, `consecutive_failures` per run, thread-safe.
  ``degraded`` when ``retries / max(calls, 1) >= 1.0`` or ``retries >= 50``.
* **Circuit breaker, per model** — after 5 consecutive fully-failed calls on a model the
  circuit for *that model* opens and the next call raises :class:`ProviderCircuitOpen`
  instead of being attempted. The run first advances the chain; only an exhausted chain
  stops the run.
* **Cross-model fallback chain** — a fixed, in-family, measured-healthy chain. The current
  position is **per run** and **sticks** for the rest of the run; `reset(run_id)` (run
  start/resume only) returns it to position 0. There is deliberately no mid-run re-probe
  of an abandoned model.

The 120 s hard LLM timeout, the retry budget and the ``finish_reason == "length"``
handling live in the provider and are untouched by this module.
"""

from __future__ import annotations

import contextvars
import os
import threading
import time
from dataclasses import dataclass, field

__all__ = [
    "MODEL_CHAIN",
    "ProviderCircuitOpen",
    "CIRCUIT_THRESHOLD",
    "MAX_FAILED_BATCHES_AFTER_EXHAUSTION",
    "model_chain",
    "current_model",
    "advance_model",
    "reset",
    "snapshot",
    "circuit_open",
    "check_circuit",
    "record_call",
    "record_retry",
    "record_success",
    "record_failure",
    "record_batch_failure",
    "record_batch_success",
    "stop_reason",
    "bind_run",
    "active_run_id",
    "max_run_seconds",
]

# Ordered, in-family fallback chain — measured live against the real NVIDIA NIM
# endpoint (0.56 s / 0.65 s / 2.03 s). `meta/llama-3.3-70b-instruct` and
# `openai/gpt-oss-120b` are deliberately EXCLUDED: both timed out at 45 s on the
# same key and host, i.e. failing over to them would be slower, not safer.
MODEL_CHAIN: list[str] = [
    "nvidia/nemotron-3-nano-30b-a3b",
    "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/nvidia-nemotron-nano-9b-v2",
]

CIRCUIT_THRESHOLD = 5
DEGRADED_RETRY_FLOOR = 50
DEGRADED_EMIT_EVERY = 50
MAX_FAILED_BATCHES_AFTER_EXHAUSTION = 3
DEFAULT_RUN_MAX_SECONDS = 3600.0

# Two batches that fail on the same model at the same moment must advance the run
# ONE step and emit ONE event. Advances arriving inside this window are coalesced.
_ADVANCE_COALESCE_S = 2.0

_PROVIDER = "nvidia"


class ProviderCircuitOpen(RuntimeError):
    """The circuit for the run's current model is open — the call was not attempted.

    Never retried by the caller: it means the model has already failed
    ``CIRCUIT_THRESHOLD`` consecutive times with its full retry budget spent.
    """

    def __init__(self, message: str = "", *, model: str | None = None, run_id: str | None = None):
        super().__init__(message or f"provider circuit open for model {model!r}")
        self.model = model
        self.run_id = run_id


# ---------------------------------------------------------------- run binding

_run_ctx: contextvars.ContextVar[tuple[str, str | None] | None] = contextvars.ContextVar(
    "llm_health_run", default=None
)


class _Binding:
    def __init__(self, token) -> None:
        self._token = token

    def __enter__(self) -> "_Binding":
        return self

    def __exit__(self, *exc) -> None:
        _run_ctx.reset(self._token)


def bind_run(run_id: str, user_id: str | None = None) -> _Binding:
    """Bind the current context to a run so the provider can attribute counters."""
    return _Binding(_run_ctx.set((run_id, user_id)))


def active_run_id() -> str | None:
    bound = _run_ctx.get()
    return bound[0] if bound else None


def _active_user_id() -> str | None:
    bound = _run_ctx.get()
    return bound[1] if bound else None


# ------------------------------------------------------------------ per-run state


@dataclass
class _RunHealth:
    run_id: str
    chain: list[str]
    user_id: str | None = None
    position: int = 0
    exhausted: bool = False
    calls: int = 0
    retries: int = 0
    consecutive_failures: int = 0
    failed_batches_after_exhaustion: int = 0
    started_at: float = field(default_factory=time.monotonic)
    last_advance_at: float = 0.0
    last_error: str = ""
    degraded_emitted_at: int = 0
    # model id -> consecutive fully-failed calls on that model
    model_failures: dict[str, int] = field(default_factory=dict)


_lock = threading.RLock()
_runs: dict[str, _RunHealth] = {}

_GLOBAL_RUN = "__global__"


def _key(run_id: str | None) -> str:
    return run_id or active_run_id() or _GLOBAL_RUN


def _state(run_id: str | None) -> _RunHealth:
    key = _key(run_id)
    with _lock:
        state = _runs.get(key)
        if state is None:
            state = _RunHealth(run_id=key, chain=model_chain(None), user_id=_active_user_id())
            _runs[key] = state
        if state.user_id is None:
            state.user_id = _active_user_id()
        return state


# ------------------------------------------------------------------ the chain


def model_chain(preferred: str | None = None) -> list[str]:
    """The full ordered chain — *preferred* first when set, then the standard chain."""
    chain: list[str] = []
    for model in ([preferred] if preferred else []) + MODEL_CHAIN:
        model = (model or "").strip()
        if model and model not in chain:
            chain.append(model)
    return chain


def current_model(run_id: str | None = None) -> str:
    """The model this run must use NOW — ``chain[chain_position]``."""
    state = _state(run_id)
    with _lock:
        index = min(state.position, len(state.chain) - 1)
        return state.chain[index]


def advance_model(
    run_id: str | None = None,
    *,
    reason: str,
    from_model: str | None = None,
    user_id: str | None = None,
) -> str | None:
    """Move this run to the next chain entry and return it; ``None`` == exhausted.

    Lock-guarded and idempotent: two batches failing on the same model advance the run
    one step and emit exactly one ``model_fallback`` event.

    Rotation applies to EVERY failure class, including ``APIConnectionError`` and
    timeouts. NVIDIA routes each model to its own backend pool, so a saturated pool for
    one model shows up as connection errors/timeouts for that model while the other
    models answer in under 2 s on the same key and host (measured live during run
    fbeed060). There is therefore no transport-error exemption and no
    ``is_model_specific()`` predicate.
    """
    state = _state(run_id)
    with _lock:
        if user_id and not state.user_id:
            state.user_id = user_id
        active = state.chain[min(state.position, len(state.chain) - 1)]
        now = time.monotonic()
        # Idempotency under concurrency. An advance zeroes the incoming model's failure
        # counter, so a second batch that failed on the model we have ALREADY left sees
        # a clean counter and is a no-op — two concurrent batches advance one step and
        # emit one event. A genuine later failure on the new model re-arms it.
        if from_model is not None and from_model != active:
            return None if state.exhausted else active
        stale = (
            from_model is None
            and state.model_failures.get(active, 0) == 0
            and state.last_advance_at
            and (now - state.last_advance_at) < _ADVANCE_COALESCE_S
        )
        if stale:
            return None if state.exhausted else active
        if state.position >= len(state.chain) - 1:
            state.exhausted = True
            state.last_error = reason
            return None
        state.position += 1
        state.last_advance_at = now
        state.last_error = reason
        target = state.chain[state.position]
        # A fresh circuit for the new model (rule E).
        state.model_failures[target] = 0
        state.consecutive_failures = 0
        emit_user = state.user_id
        run_key = state.run_id

    _emit_model_fallback(emit_user, run_key, active, target, reason)
    return target


def chain_exhausted(run_id: str | None = None) -> bool:
    return _state(run_id).exhausted


# --------------------------------------------------------------- counters / circuit


def record_call(run_id: str | None = None) -> None:
    state = _state(run_id)
    with _lock:
        state.calls += 1


def record_retry(run_id: str | None = None, *, model: str | None = None) -> None:
    """One retry of an in-flight call. A throttle wait is NEVER recorded here."""
    state = _state(run_id)
    with _lock:
        state.retries += 1
        should_emit = _is_degraded(state) and (
            state.retries - state.degraded_emitted_at >= DEGRADED_EMIT_EVERY
            or state.degraded_emitted_at == 0
        )
        if should_emit:
            state.degraded_emitted_at = state.retries
        payload = (
            state.user_id,
            state.run_id,
            model or state.chain[min(state.position, len(state.chain) - 1)],
            state.calls,
            state.retries,
            state.consecutive_failures,
        )
    if should_emit:
        _emit_provider_degraded(*payload)


def record_success(run_id: str | None = None, *, model: str | None = None) -> None:
    state = _state(run_id)
    with _lock:
        state.consecutive_failures = 0
        if model:
            state.model_failures[model] = 0
        else:
            state.model_failures.clear()


def record_failure(
    run_id: str | None = None, *, model: str | None = None, error: str = ""
) -> None:
    """One fully-failed call (its whole retry budget spent)."""
    state = _state(run_id)
    with _lock:
        state.consecutive_failures += 1
        if error:
            state.last_error = error
        target = model or state.chain[min(state.position, len(state.chain) - 1)]
        state.model_failures[target] = state.model_failures.get(target, 0) + 1


def circuit_open(run_id: str | None = None, *, model: str | None = None) -> bool:
    """True when the circuit for *model* (default: the run's current model) is open."""
    state = _state(run_id)
    with _lock:
        target = model or state.chain[min(state.position, len(state.chain) - 1)]
        return state.model_failures.get(target, 0) >= CIRCUIT_THRESHOLD


def check_circuit(run_id: str | None = None, *, model: str | None = None) -> None:
    """Raise :class:`ProviderCircuitOpen` instead of attempting a doomed call."""
    if circuit_open(run_id, model=model):
        state = _state(run_id)
        target = model or current_model(run_id)
        raise ProviderCircuitOpen(
            f"circuit open for {target!r} after {CIRCUIT_THRESHOLD} consecutive failed "
            f"calls (last: {state.last_error or 'unknown error'})",
            model=target,
            run_id=state.run_id,
        )


def is_degraded(run_id: str | None = None) -> bool:
    return _is_degraded(_state(run_id))


def _is_degraded(state: _RunHealth) -> bool:
    return state.retries / max(state.calls, 1) >= 1.0 or state.retries >= DEGRADED_RETRY_FLOOR


# ------------------------------------------------------------------ never stuck


def max_run_seconds() -> float:
    raw = os.getenv("AGENT_RUN_MAX_SECONDS", "")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_RUN_MAX_SECONDS
    return value if value > 0 else DEFAULT_RUN_MAX_SECONDS


def record_batch_failure(run_id: str | None = None, *, error: str = "") -> None:
    state = _state(run_id)
    with _lock:
        if error:
            state.last_error = error
        if state.exhausted:
            state.failed_batches_after_exhaustion += 1


def record_batch_success(run_id: str | None = None) -> None:
    state = _state(run_id)
    with _lock:
        state.failed_batches_after_exhaustion = 0


def stop_reason(run_id: str | None = None) -> str | None:
    """A human-readable reason to end the run ``resumable``, or ``None`` to continue.

    Two bounds, either of which ends a run that would otherwise grind (rule H).
    """
    state = _state(run_id)
    with _lock:
        elapsed = time.monotonic() - state.started_at
        limit = max_run_seconds()
        if elapsed >= limit:
            return (
                f"Run exceeded the {int(limit)}s wall-clock ceiling "
                f"(AGENT_RUN_MAX_SECONDS) after {int(elapsed)}s. "
                "Nothing was left half-applied. Resume to continue."
            )
        if (
            state.exhausted
            and state.failed_batches_after_exhaustion >= MAX_FAILED_BATCHES_AFTER_EXHAUSTION
        ):
            return (
                f"All {len(state.chain)} models failed "
                f"({', '.join(state.chain)}; last: {state.last_error or 'unknown error'}). "
                f"Stopped after {state.failed_batches_after_exhaustion} further failed "
                "batches. Nothing was left half-applied. Resume to continue."
            )
        return None


# ------------------------------------------------------------------ lifecycle


def reset(run_id: str, *, preferred: str | None = None, user_id: str | None = None) -> None:
    """Clear this run's counters, circuit and chain position — start/resume only."""
    with _lock:
        _runs[run_id] = _RunHealth(
            run_id=run_id,
            chain=model_chain(preferred),
            user_id=user_id or _active_user_id(),
        )


def reset_all() -> None:
    """Test helper: drop all per-run health state."""
    with _lock:
        _runs.clear()


def snapshot(run_id: str | None = None) -> dict:
    """The health payload behind ``GET /api/provider-health``."""
    from llm import throttle

    state = _state(run_id)
    with _lock:
        position = min(state.position, len(state.chain) - 1)
        return {
            "provider": _PROVIDER,
            "model": state.chain[position],
            "model_chain": list(state.chain),
            "chain_position": position,
            "chain_exhausted": state.exhausted,
            "circuit_open": state.model_failures.get(state.chain[position], 0)
            >= CIRCUIT_THRESHOLD,
            "calls": state.calls,
            "retries": state.retries,
            "consecutive_failures": state.consecutive_failures,
            "degraded": _is_degraded(state),
            "run_id": None if state.run_id == _GLOBAL_RUN else state.run_id,
            "throttle": throttle.snapshot(),
        }


# ------------------------------------------------------------------ event bridge
#
# Slice 2 owns `events.bus.emit_model_fallback` / `emit_provider_degraded`. Import
# defensively so this module never hard-depends on it landing first, and never let a
# telemetry failure break a run.


def _emit_model_fallback(user_id, run_id, from_model, to_model, reason) -> None:
    if not user_id:
        return
    try:
        from events.bus import emit_model_fallback  # type: ignore[attr-defined]
    except (ImportError, AttributeError):
        return
    try:
        emit_model_fallback(
            user_id, run_id=run_id, from_model=from_model, to_model=to_model, reason=reason
        )
    except Exception:  # pragma: no cover - telemetry must never break a run
        pass


def _emit_provider_degraded(user_id, run_id, model, calls, retries, consecutive_failures) -> None:
    if not user_id:
        return
    try:
        from events.bus import emit_provider_degraded  # type: ignore[attr-defined]
    except (ImportError, AttributeError):
        return
    try:
        emit_provider_degraded(
            user_id,
            run_id=run_id,
            provider=_PROVIDER,
            model=model,
            calls=calls,
            retries=retries,
            consecutive_failures=consecutive_failures,
        )
    except Exception:  # pragma: no cover
        pass
