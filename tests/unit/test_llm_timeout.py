"""Hard timeouts: a stalled provider is impossible by construction.

The old build hung on NVIDIA — a slowly-streaming model ran far past the httpx
read timeout. These tests stall the transport at the SDK boundary and assert the
`asyncio.wait_for` deadline cuts the call off within budget, and that a stalled
primary fails over to the fallback.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from llm.client import LLMClient
from llm.providers.base import LLMError, LLMResult
from llm.providers.gemini import GeminiProvider
from llm.providers.nvidia import NvidiaProvider


class _StallingCompletions:
    async def create(self, **kwargs):
        await asyncio.sleep(3600)  # a provider that never answers


class _StallingClient:
    class chat:  # noqa: N801 - mimics the openai SDK surface
        completions = _StallingCompletions()


def _stalling_nvidia(timeout_s: float = 0.2) -> NvidiaProvider:
    return NvidiaProvider(
        api_key="test-key",
        base_url="https://example.invalid/v1",
        default_model="nvidia/nemotron-3-nano-30b-a3b",
        timeout_s=timeout_s,
        max_retries=1,
        client=_StallingClient(),
    )


@pytest.mark.timeout(10)
async def test_a_stalled_nvidia_call_is_cut_off_at_the_hard_deadline():
    provider = _stalling_nvidia(timeout_s=0.2)
    started = time.monotonic()
    with pytest.raises(LLMError, match="TimeoutError"):
        await provider.call_model("hello", max_tokens=16)
    assert time.monotonic() - started < 5.0, "the hard deadline must bound wall-clock time"


@pytest.mark.timeout(10)
async def test_a_stalled_gemini_call_is_cut_off_at_the_hard_deadline():
    provider = GeminiProvider(
        api_key="test-key",
        default_model="gemini-2.5-flash-lite",
        timeout_s=0.2,
        max_retries=1,
        client=_StallingClient(),
    )
    started = time.monotonic()
    with pytest.raises(LLMError):
        await provider.call_model("hello", max_tokens=16)
    assert time.monotonic() - started < 5.0


@pytest.mark.timeout(10)
async def test_a_stalled_primary_fails_over_to_the_fallback():
    class _InstantFallback:
        name = "gemini"
        default_model = "gemini-2.5-flash-lite"

        async def call_model(self, prompt, **kwargs):
            return LLMResult(text="PONG", model=self.default_model, provider="gemini")

    client = LLMClient(primary=_stalling_nvidia(0.2), fallback=_InstantFallback())
    started = time.monotonic()
    result = await client.call_model("ping", max_tokens=8)
    assert result.text == "PONG"
    assert result.fallback is True
    assert time.monotonic() - started < 5.0


def test_missing_api_key_fails_fast_with_the_env_var_name():
    with pytest.raises(LLMError, match="AGENT_NVIDIA_API_KEY"):
        NvidiaProvider(api_key="", base_url="x", default_model="m")
    with pytest.raises(LLMError, match="AGENT_GEMINI_API_KEY"):
        GeminiProvider(api_key="", default_model="m")
