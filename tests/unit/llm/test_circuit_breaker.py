"""Rule E — per-model circuit breaker, health counters and the degraded thresholds."""

from __future__ import annotations

import httpx
import pytest
from openai import APIConnectionError

from llm import health, throttle
from llm.providers.base import LLMError
from llm.providers.nvidia import DEFAULT_TIMEOUT_S, NvidiaProvider
from tests.unit.llm.fakes import FakeOpenAIClient, FakeResponse

MODEL = "vendor/default-model"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # A wide-open bucket keeps these tests about the breaker, not about pacing.
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


def _provider(script) -> tuple[NvidiaProvider, FakeOpenAIClient]:
    fake = FakeOpenAIClient(script)
    return (
        NvidiaProvider(
            api_key="nvapi-test",
            base_url="https://integrate.api.nvidia.com/v1",
            default_model=MODEL,
            client=fake,
            max_retries=1,
        ),
        fake,
    )


async def test_circuit_opens_after_exactly_five_consecutive_failures():
    provider, fake = _provider([_conn_error() for _ in range(20)])
    with health.bind_run("run-cb", "user-alice"):
        for _ in range(health.CIRCUIT_THRESHOLD):
            with pytest.raises(LLMError):
                await provider.call_model("hi")
        assert health.circuit_open("run-cb", model=MODEL) is True
        attempts_before = len(fake.calls)

        # The 6th call is refused BEFORE any HTTP attempt.
        with pytest.raises(health.ProviderCircuitOpen) as exc:
            await provider.call_model("hi")

    assert len(fake.calls) == attempts_before
    assert exc.value.model == MODEL
    assert not isinstance(exc.value, LLMError)  # never swallowed by a retry/degrade path


async def test_circuit_stays_closed_below_the_threshold():
    provider, _ = _provider([_conn_error() for _ in range(20)])
    with health.bind_run("run-cb2", "user-alice"):
        for _ in range(health.CIRCUIT_THRESHOLD - 1):
            with pytest.raises(LLMError):
                await provider.call_model("hi")
        assert health.circuit_open("run-cb2", model=MODEL) is False
        health.check_circuit("run-cb2", model=MODEL)  # does not raise


async def test_a_success_resets_consecutive_failures():
    script = [_conn_error() for _ in range(3)] + [FakeResponse("ok", model=MODEL)]
    provider, _ = _provider(script)
    with health.bind_run("run-cb3", "user-alice"):
        for _ in range(3):
            with pytest.raises(LLMError):
                await provider.call_model("hi")
        assert health.snapshot("run-cb3")["consecutive_failures"] == 3
        await provider.call_model("hi")

    snap = health.snapshot("run-cb3")
    assert snap["consecutive_failures"] == 0
    assert health.circuit_open("run-cb3", model=MODEL) is False


async def test_reset_clears_the_open_circuit_so_a_resume_gets_a_fresh_attempt():
    provider, _ = _provider([_conn_error() for _ in range(20)])
    with health.bind_run("run-cb4", "user-alice"):
        for _ in range(health.CIRCUIT_THRESHOLD):
            with pytest.raises(LLMError):
                await provider.call_model("hi")
        assert health.circuit_open("run-cb4", model=MODEL) is True

        health.reset("run-cb4")
        assert health.circuit_open("run-cb4", model=MODEL) is False
        assert health.snapshot("run-cb4")["chain_position"] == 0


def test_degraded_flips_at_the_documented_thresholds():
    health.reset("run-deg")
    for _ in range(10):
        health.record_call("run-deg")
    for _ in range(9):
        health.record_retry("run-deg")
    assert health.snapshot("run-deg")["degraded"] is False

    health.record_retry("run-deg")  # retries/calls == 1.0
    assert health.snapshot("run-deg")["degraded"] is True

    # The other threshold on its own: 50 retries, whatever the call count.
    health.reset("run-deg2")
    for _ in range(1000):
        health.record_call("run-deg2")
    for _ in range(49):
        health.record_retry("run-deg2")
    assert health.snapshot("run-deg2")["degraded"] is False
    health.record_retry("run-deg2")
    assert health.snapshot("run-deg2")["degraded"] is True


async def test_provider_records_calls_and_retries_on_the_run():
    script = [_conn_error(), FakeResponse("ok", model=MODEL)]
    fake = FakeOpenAIClient(script)
    provider = NvidiaProvider(
        api_key="nvapi-test",
        base_url="https://integrate.api.nvidia.com/v1",
        default_model=MODEL,
        client=fake,
        max_retries=2,
    )
    with health.bind_run("run-count", "user-alice"):
        await provider.call_model("hi")

    snap = health.snapshot("run-count")
    assert snap["calls"] == 1
    assert snap["retries"] == 1
    assert snap["consecutive_failures"] == 0


def test_the_120s_hard_timeout_constant_is_unchanged():
    # Phase 6 must not touch the hard wall-clock deadline per attempt.
    assert DEFAULT_TIMEOUT_S == 120.0
