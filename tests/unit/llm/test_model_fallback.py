"""Rule F — the in-family fallback chain, and rotation on ANY persistent failure."""

from __future__ import annotations

import asyncio
import threading

import httpx
import pytest
from openai import APIConnectionError, APIStatusError

from llm import health, throttle
from llm.providers.base import LLMError
from llm.providers.nvidia import NvidiaProvider
from tests.unit.llm.fakes import FakeOpenAIClient, FakeResponse

PRIMARY = "nvidia/nemotron-3-nano-30b-a3b"
SECOND = "nvidia/nemotron-3-super-120b-a12b"
THIRD = "nvidia/nvidia-nemotron-nano-9b-v2"

EXCLUDED = ("meta/llama-3.3-70b-instruct", "openai/gpt-oss-120b")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("AGENT_LLM_MAX_RPM", "490")
    throttle.reset()
    health.reset_all()
    yield
    throttle.reset()
    health.reset_all()


@pytest.fixture
def events(monkeypatch):
    """Capture what slice 2's `emit_model_fallback` would receive."""
    captured: list[dict] = []

    def _emit(user_id, run_id, from_model, to_model, reason):
        captured.append(
            {
                "user_id": user_id,
                "run_id": run_id,
                "from_model": from_model,
                "to_model": to_model,
                "reason": reason,
            }
        )

    monkeypatch.setattr(health, "_emit_model_fallback", _emit)
    return captured


# --- the chain itself ---------------------------------------------------


def test_default_chain_is_the_three_measured_healthy_in_family_models():
    assert health.model_chain(None) == [PRIMARY, SECOND, THIRD]


def test_chain_puts_the_user_preference_first_and_dedupes():
    assert health.model_chain("x/y") == ["x/y", PRIMARY, SECOND, THIRD]
    assert health.model_chain(SECOND) == [SECOND, PRIMARY, THIRD]
    assert health.model_chain("") == [PRIMARY, SECOND, THIRD]


def test_no_out_of_family_model_appears_in_any_chain():
    # Both timed out at 45 s on the same key/endpoint — worse than the primary.
    for chain in (health.model_chain(None), health.model_chain("x/y")):
        for excluded in EXCLUDED:
            assert excluded not in chain


def test_the_primary_is_the_unchanged_default_model():
    from config.settings import get_settings

    assert health.model_chain(None)[0] == get_settings().nvidia_default_model == PRIMARY


# --- advancing ----------------------------------------------------------


def test_advance_walks_the_chain_then_reports_exhaustion(events):
    health.reset("run-adv", user_id="user-alice")
    assert health.current_model("run-adv") == PRIMARY

    health.record_failure("run-adv", model=PRIMARY, error="404")
    assert health.advance_model("run-adv", reason="404") == SECOND
    assert health.current_model("run-adv") == SECOND

    health.record_failure("run-adv", model=SECOND, error="APIConnectionError")
    assert health.advance_model("run-adv", reason="APIConnectionError") == THIRD

    health.record_failure("run-adv", model=THIRD, error="APIConnectionError")
    assert health.advance_model("run-adv", reason="APIConnectionError") is None
    assert health.chain_exhausted("run-adv") is True
    assert [(e["from_model"], e["to_model"]) for e in events] == [
        (PRIMARY, SECOND),
        (SECOND, THIRD),
    ]


def test_the_advance_is_per_run_and_sticks(events):
    health.reset("run-a", user_id="user-alice")
    health.reset("run-b", user_id="user-alice")
    health.record_failure("run-a", model=PRIMARY, error="boom")
    health.advance_model("run-a", reason="APIConnectionError after 3 retries")

    # Sticky for the rest of run-a, whatever batch/tier asks next...
    assert health.current_model("run-a") == SECOND
    assert health.current_model("run-a") == SECOND
    # ...and never leaks into another run.
    assert health.current_model("run-b") == PRIMARY

    # Only a reset (run start/resume) goes back to the primary.
    health.reset("run-a")
    assert health.current_model("run-a") == PRIMARY


def test_two_concurrent_batches_advance_exactly_one_step_and_emit_one_event(events):
    health.reset("run-conc", user_id="user-alice")
    barrier = threading.Barrier(8)
    results: list[str | None] = []

    def _fail():
        barrier.wait(timeout=10)
        results.append(health.advance_model("run-conc", reason="APIConnectionError"))

    threads = [threading.Thread(target=_fail) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert health.current_model("run-conc") == SECOND
    assert health.snapshot("run-conc")["chain_position"] == 1
    assert len(events) == 1
    assert events[0]["reason"] == "APIConnectionError"
    assert all(r == SECOND for r in results)


def test_advance_reporting_an_already_abandoned_model_is_a_no_op(events):
    health.reset("run-stale", user_id="user-alice")
    health.record_failure("run-stale", model=PRIMARY, error="404")
    health.advance_model("run-stale", reason="404")
    assert health.advance_model("run-stale", reason="404", from_model=PRIMARY) == SECOND
    assert health.current_model("run-stale") == SECOND
    assert len(events) == 1


# --- rotation on every failure class ------------------------------------


def _status_error(code: int) -> APIStatusError:
    request = httpx.Request("POST", "https://integrate.api.nvidia.com/v1/chat/completions")
    return APIStatusError(f"status {code}", response=httpx.Response(code, request=request), body=None)


def _conn_error() -> APIConnectionError:
    return APIConnectionError(
        request=httpx.Request("POST", "https://integrate.api.nvidia.com/v1/chat/completions")
    )


class _PerModelClient:
    """Fails every call for `bad_model`, serves everything else. Records requests."""

    def __init__(self, bad_model: str, error_factory) -> None:
        self.requested: list[str] = []
        outer = self

        class _Completions:
            async def create(self, **kwargs):
                outer.requested.append(kwargs["model"])
                if kwargs["model"] == bad_model:
                    raise error_factory()
                return FakeResponse("ok", model=kwargs["model"])

        self.chat = type("Chat", (), {})()
        self.chat.completions = _Completions()


def _provider(client) -> NvidiaProvider:
    return NvidiaProvider(
        api_key="nvapi-test",
        base_url="https://integrate.api.nvidia.com/v1",
        default_model=PRIMARY,
        client=client,
        max_retries=1,
    )


async def _run_batches(provider, run_id: str, n: int) -> list[str]:
    """Drive `n` batches the way slice 1's candidate loop does."""
    served: list[str] = []
    with health.bind_run(run_id, "user-alice"):
        for _ in range(n):
            while True:
                model = health.current_model(run_id)
                try:
                    result = await provider.call_model("classify", model=model)
                except (LLMError, health.ProviderCircuitOpen) as exc:
                    nxt = health.advance_model(
                        run_id,
                        reason=f"{type(exc.__cause__ or exc).__name__} after retries",
                        from_model=model,
                    )
                    if nxt is None:
                        raise
                    continue
                served.append(result.model)
                break
    return served


async def test_a_404_on_the_primary_rotates_and_the_batch_completes_on_model_two(events):
    client = _PerModelClient(PRIMARY, lambda: _status_error(404))
    health.reset("run-404", user_id="user-alice")

    served = await _run_batches(_provider(client), "run-404", 3)

    assert served == [SECOND, SECOND, SECOND]
    assert len(events) == 1
    assert (events[0]["from_model"], events[0]["to_model"]) == (PRIMARY, SECOND)
    # Cost honesty: the model that ACTUALLY served the call is what is reported.
    assert client.requested.count(PRIMARY) == 1


async def test_a_connection_error_on_the_primary_also_rotates_and_never_re_requests_it(events):
    # NVIDIA routes per model to separate backend pools: a saturated pool for one
    # model looks exactly like a transport failure while the others are healthy.
    client = _PerModelClient(PRIMARY, _conn_error)
    health.reset("run-conn", user_id="user-alice")

    served = await _run_batches(_provider(client), "run-conn", 5)

    assert served == [SECOND] * 5
    assert len(events) == 1
    assert "APIConnectionError" in events[0]["reason"]
    # The advance sticks per-run: the primary is never requested again.
    assert client.requested.index(PRIMARY) == 0
    assert client.requested.count(PRIMARY) == 1
    assert health.chain_exhausted("run-conn") is False


async def test_when_every_model_fails_the_chain_is_reported_exhausted(events):
    class _AllFail:
        def __init__(self):
            self.requested = []
            outer = self

            class _C:
                async def create(self, **kwargs):
                    outer.requested.append(kwargs["model"])
                    raise _conn_error()

            self.chat = type("Chat", (), {})()
            self.chat.completions = _C()

    client = _AllFail()
    health.reset("run-all", user_id="user-alice")

    with pytest.raises(LLMError):
        await asyncio.wait_for(_run_batches(_provider(client), "run-all", 1), timeout=30)

    assert health.chain_exhausted("run-all") is True
    assert set(client.requested) == {PRIMARY, SECOND, THIRD}
    assert len(events) == 2


async def test_llm_result_model_is_the_model_that_actually_served_the_call():
    fake = FakeOpenAIClient([FakeResponse("ok", model=SECOND)])
    result = await _provider(fake).call_model("hi", model=PRIMARY)
    assert result.model == SECOND  # response.model wins over the requested id
