"""NvidiaProvider — model swappability, accounting, retries, error paths."""

from __future__ import annotations

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, RateLimitError

from llm.providers.base import LLMError, LLMResult, estimate_cost_usd
from llm.providers.nvidia import NvidiaProvider
from tests.unit.llm.fakes import FakeOpenAIClient, FakeResponse


def _provider(script=(), **kwargs) -> tuple[NvidiaProvider, FakeOpenAIClient]:
    fake = FakeOpenAIClient(script)
    provider = NvidiaProvider(
        api_key="nvapi-test",
        base_url="https://integrate.api.nvidia.com/v1",
        default_model="vendor/default-model",
        client=fake,
        **kwargs,
    )
    return provider, fake


def _status_error(code: int) -> APIStatusError:
    request = httpx.Request("POST", "https://integrate.api.nvidia.com/v1/chat/completions")
    response = httpx.Response(code, request=request)
    return (RateLimitError if code == 429 else APIStatusError)(
        f"status {code}", response=response, body=None
    )


# --- happy path ---------------------------------------------------------


async def test_call_model_returns_text_and_full_accounting():
    provider, fake = _provider([FakeResponse("hello there", model="vendor/default-model")])
    result = await provider.call_model("say hi", system="be terse")

    assert isinstance(result, LLMResult)
    assert result.text == "hello there"
    assert result.model == "vendor/default-model"
    assert (result.tokens_in, result.tokens_out) == (11, 7)
    assert result.tokens_total == 18
    assert result.latency_ms >= 0
    assert result.usd == estimate_cost_usd("vendor/default-model", 11, 7)
    assert result.attempts == 1

    sent = fake.calls[0]
    assert sent["model"] == "vendor/default-model"
    assert sent["messages"] == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "say hi"},
    ]


async def test_model_is_swappable_per_call_and_never_hardcoded():
    provider, fake = _provider([FakeResponse("a"), FakeResponse("b")])
    await provider.call_model("one")
    await provider.call_model("two", model="vendor/other-model")

    assert [c["model"] for c in fake.calls] == ["vendor/default-model", "vendor/other-model"]
    assert provider.default_model == "vendor/default-model"


async def test_json_schema_is_passed_through_as_response_format():
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    provider, fake = _provider(['{"ok": true}'])
    await provider.call_model("go", json_schema=schema)
    assert fake.calls[0]["response_format"]["json_schema"]["schema"] == schema


async def test_falls_back_to_reasoning_content_when_content_is_empty():
    response = FakeResponse(None)
    response.choices[0].message.reasoning_content = "thought text"
    provider, _ = _provider([response])
    assert (await provider.call_model("go")).text == "thought text"


# --- edge cases ---------------------------------------------------------


async def test_empty_prompt_is_rejected_before_any_network_call():
    provider, fake = _provider()
    with pytest.raises(ValueError, match="non-empty"):
        await provider.call_model("   ")
    assert fake.calls == []


async def test_missing_api_key_fails_loudly_at_construction():
    with pytest.raises(LLMError, match="AGENT_NVIDIA_API_KEY"):
        NvidiaProvider(api_key="", base_url="https://x/v1", default_model="m")


async def test_missing_default_model_fails_loudly():
    with pytest.raises(LLMError, match="AGENT_NVIDIA_DEFAULT_MODEL"):
        NvidiaProvider(api_key="k", base_url="https://x/v1", default_model="")


async def test_missing_usage_block_degrades_to_zero_tokens():
    response = FakeResponse("text")
    response.usage = None
    provider, _ = _provider([response])
    result = await provider.call_model("go")
    assert (result.tokens_in, result.tokens_out, result.usd) == (0, 0, 0.0)


# --- error paths --------------------------------------------------------


async def test_retries_on_429_then_succeeds(monkeypatch):
    monkeypatch.setattr("llm.providers.nvidia.asyncio.sleep", _no_sleep)
    provider, fake = _provider([_status_error(429), FakeResponse("recovered")])
    result = await provider.call_model("go")
    assert result.text == "recovered"
    assert result.attempts == 2
    assert len(fake.calls) == 2


async def test_retries_connection_errors_then_raises_llm_error(monkeypatch):
    monkeypatch.setattr("llm.providers.nvidia.asyncio.sleep", _no_sleep)
    boom = APIConnectionError(request=httpx.Request("POST", "https://x/v1"))
    provider, fake = _provider([boom, boom, boom], max_retries=3)
    with pytest.raises(LLMError, match="after 3 attempts"):
        await provider.call_model("go")
    assert len(fake.calls) == 3


async def test_non_retryable_status_fails_immediately(monkeypatch):
    monkeypatch.setattr("llm.providers.nvidia.asyncio.sleep", _no_sleep)
    provider, fake = _provider([_status_error(404), FakeResponse("never used")])
    with pytest.raises(LLMError, match="404"):
        await provider.call_model("go", model="vendor/missing-model")
    assert len(fake.calls) == 1


async def _no_sleep(_seconds):
    return None
