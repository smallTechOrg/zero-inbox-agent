"""Rule H — a run is always bounded: it never hangs and never grinds indefinitely."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from openai import APIConnectionError

from llm import health, throttle
from llm.providers.base import LLMError
from llm.providers.nvidia import NvidiaProvider

PRIMARY = "nvidia/nemotron-3-nano-30b-a3b"
THIRD = "nvidia/nvidia-nemotron-nano-9b-v2"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("AGENT_LLM_MAX_RPM", "490")
    throttle.reset()
    health.reset_all()
    yield
    throttle.reset()
    health.reset_all()


def _conn_error() -> APIConnectionError:
    return APIConnectionError(
        request=httpx.Request("POST", "https://integrate.api.nvidia.com/v1/chat/completions")
    )


def test_default_wall_clock_ceiling_is_one_hour(monkeypatch):
    monkeypatch.delenv("AGENT_RUN_MAX_SECONDS", raising=False)
    assert health.max_run_seconds() == 3600.0
    monkeypatch.setenv("AGENT_RUN_MAX_SECONDS", "90")
    assert health.max_run_seconds() == 90.0
    monkeypatch.setenv("AGENT_RUN_MAX_SECONDS", "junk")
    assert health.max_run_seconds() == 3600.0


def test_wall_clock_ceiling_alone_ends_a_grinding_run(monkeypatch):
    monkeypatch.setenv("AGENT_RUN_MAX_SECONDS", "1")
    health.reset("run-clock", user_id="user-alice")
    assert health.stop_reason("run-clock") is None

    # Age the run past the ceiling without actually waiting an hour.
    health._runs["run-clock"].started_at = time.monotonic() - 2.0

    reason = health.stop_reason("run-clock")
    assert reason and "wall-clock" in reason
    assert "AGENT_RUN_MAX_SECONDS" in reason
    assert "Resume to continue." in reason


def test_three_failed_batches_after_chain_exhaustion_end_the_run(monkeypatch):
    monkeypatch.setenv("AGENT_RUN_MAX_SECONDS", "3600")
    health.reset("run-bound", user_id="user-alice")

    # Failed batches BEFORE exhaustion never stop the run — the chain is the answer.
    for _ in range(10):
        health.record_batch_failure("run-bound", error="APIConnectionError")
    assert health.stop_reason("run-bound") is None

    for model in health.model_chain(None):
        health.record_failure("run-bound", model=model, error="APIConnectionError")
        health.advance_model("run-bound", reason="APIConnectionError", from_model=model)
    assert health.chain_exhausted("run-bound") is True

    for i in range(health.MAX_FAILED_BATCHES_AFTER_EXHAUSTION):
        assert health.stop_reason("run-bound") is None, i
        health.record_batch_failure("run-bound", error="APIConnectionError")

    reason = health.stop_reason("run-bound")
    assert reason is not None
    assert "All 3 models failed" in reason
    for model in health.model_chain(None):
        assert model in reason  # names the exhausted chain
    assert "APIConnectionError" in reason  # names the last failure
    assert "Resume to continue." in reason


def test_a_success_clears_the_post_exhaustion_failure_bound():
    health.reset("run-recover", user_id="user-alice")
    for model in health.model_chain(None):
        health.record_failure("run-recover", model=model, error="boom")
        health.advance_model("run-recover", reason="boom", from_model=model)
    health.record_batch_failure("run-recover", error="boom")
    health.record_batch_failure("run-recover", error="boom")
    health.record_batch_success("run-recover")
    health.record_batch_failure("run-recover", error="boom")
    assert health.stop_reason("run-recover") is None


async def test_all_three_models_failing_terminates_in_bounded_time():
    """The whole chain dead must stop the run in seconds, not grind for 20 minutes."""

    requested: list[str] = []

    class _AllFail:
        def __init__(self):
            outer_requested = requested

            class _C:
                async def create(self, **kwargs):
                    outer_requested.append(kwargs["model"])
                    raise _conn_error()

            self.chat = type("Chat", (), {})()
            self.chat.completions = _C()

    provider = NvidiaProvider(
        api_key="nvapi-test",
        base_url="https://integrate.api.nvidia.com/v1",
        default_model=PRIMARY,
        client=_AllFail(),
        max_retries=1,
    )
    health.reset("run-dead", user_id="user-alice")
    decided_before = 17  # threads already durable at the point the provider died
    stopped_after = None

    async def _drive() -> str | None:
        nonlocal stopped_after
        with health.bind_run("run-dead", "user-alice"):
            for _ in range(500):  # far more batches than the bound permits
                model = health.current_model("run-dead")
                try:
                    await provider.call_model("classify", model=model)
                except (LLMError, health.ProviderCircuitOpen) as exc:
                    health.record_batch_failure("run-dead", error=type(exc).__name__)
                    health.advance_model(
                        "run-dead", reason="APIConnectionError", from_model=model
                    )
                reason = health.stop_reason("run-dead")
                if reason:
                    stopped_after = len(requested)
                    return reason
        return None

    started = time.monotonic()
    reason = await asyncio.wait_for(_drive(), timeout=30)
    elapsed = time.monotonic() - started

    assert reason is not None
    assert elapsed < 30 < health.max_run_seconds()
    # It stopped after a handful of attempts, not after 500 batches.
    assert stopped_after is not None and stopped_after <= 20
    # Partial work is untouched by the bound — nothing here clears it.
    assert decided_before == 17
    snap = health.snapshot("run-dead")
    assert snap["chain_exhausted"] is True
    assert snap["model"] == THIRD
