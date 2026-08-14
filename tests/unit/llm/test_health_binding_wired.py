"""`llm.health.bind_run` must actually be wired into a run — not dead code.

Regression: bind_run was defined and exported but never called anywhere in
src/, so every provider_degraded / model_fallback event emitted during a real
run carried no user_id and was silently dropped instead of reaching the
activity feed — the exact "invisible backend action" this capability exists to
fix. A second, subtler half: graph nodes run their async LLM calls in a worker
thread, and contextvars do NOT cross a thread boundary on their own, so the
binding also has to be carried across explicitly.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars


def test_bind_run_is_called_by_the_runner_not_merely_defined():
    """The runner must open the binding around the graph invocation."""
    import inspect

    from graph import runner

    source = inspect.getsource(runner)
    assert "_bind_provider_health" in source
    # and it must actually wrap the invoke, not just exist
    assert "with _bind_provider_health(" in source


def test_the_binding_survives_the_worker_thread_hop():
    """graph.nodes._run_async offloads to a ThreadPoolExecutor. Without an
    explicit contextvars.copy_context() the bound run is invisible there, and
    every provider event is emitted unattributed."""
    from llm import health

    async def _probe():
        return health.active_run_id(), health._active_user_id()

    with health.bind_run("run-bound", "user-bound"):
        ctx = contextvars.copy_context()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            run_id, user_id = pool.submit(ctx.run, asyncio.run, _probe()).result(timeout=10)

    assert run_id == "run-bound"
    assert user_id == "user-bound"


def test_run_async_carries_the_binding_into_its_worker_thread():
    """The real _run_async, not a hand-rolled copy — proves the production path."""
    from graph.nodes import _run_async
    from llm import health

    async def _probe():
        return health.active_run_id()

    # Force the threadpool branch by calling from inside a running loop.
    async def _outer():
        return _run_async(_probe(), timeout=10)

    with health.bind_run("run-real", "user-real"):
        got = asyncio.run(_outer())

    assert got == "run-real", (
        "the bound run did not survive _run_async's thread hop — provider "
        "events during a real run would be emitted unattributed and dropped"
    )


def test_a_model_fallback_during_a_bound_run_reaches_that_users_feed():
    """End to end: rotate the model inside a binding, assert the SSE event is
    attributed to the right user (i.e. actually deliverable)."""
    from events import bus
    from llm import health

    user_id = "user-feed"
    health.reset("run-feed", user_id=user_id)

    with health.bind_run("run-feed", user_id):
        health.advance_model("run-feed", reason="probe: forced rotation")

    events = bus.replay_buffer(user_id)
    fallbacks = [e for e in events if e.get("type") == "model_fallback"]
    assert fallbacks, (
        f"no model_fallback reached user {user_id!r}'s feed — got types "
        f"{[e.get('type') for e in events]}"
    )
    assert fallbacks[-1].get("run_id") == "run-feed"
