"""Reasoning-model discipline — regressions for the Phase-1 tier-3 blocker.

Nemotron is a reasoning model: chain-of-thought lands in `reasoning_content` and
can consume the whole max_tokens budget, leaving `content` empty with
finish_reason=="length". These tests pin the fixed behaviour:

1. classify_batch sends the JSON schema as a real response_format,
2. thinking is disabled for structured calls,
3. finish_reason=="length" splits the batch (or doubles a single item's budget)
   instead of parse-and-retrying the same oversized request,
4. reasoning_content is NEVER used as the answer of a JSON-schema call,
5. a wall-clock deadline bounds each attempt,
6. a fully-failed batch still reports its token spend.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from fakes import FakeOpenAIClient, FakeResponse

from llm.client import LLMClient
from llm.providers.base import LLMError, LLMSchemaError
from llm.providers.nvidia import NvidiaProvider

ITEM_SCHEMA = {
    "type": "object",
    "required": ["item_id", "category"],
    "additionalProperties": True,
    "properties": {
        "item_id": {"type": "string"},
        "category": {"type": "string", "minLength": 1},
    },
}

INSTRUCTIONS = "Classify each thread."


def _items(n: int) -> list[dict]:
    return [{"item_id": f"t{i}", "subject": f"Subject {i}"} for i in range(n)]


def _payload(*ids: str) -> str:
    return json.dumps({"results": [{"item_id": i, "category": "news"} for i in ids]})


def _client(script) -> tuple[LLMClient, FakeOpenAIClient]:
    fake = FakeOpenAIClient(script)
    provider = NvidiaProvider(
        api_key="nvapi-test",
        base_url="https://integrate.api.nvidia.com/v1",
        default_model="vendor/default-model",
        client=fake,
    )
    return LLMClient(provider), fake


# --- structured output really reaches the wire (defect 1a / 1b) ----------


async def test_classify_batch_sends_response_format_json_schema():
    client, fake = _client([_payload("t0", "t1")])
    await client.classify_batch(_items(2), instructions=INSTRUCTIONS, item_schema=ITEM_SCHEMA)

    sent = fake.calls[0]
    response_format = sent["response_format"]
    assert response_format["type"] == "json_schema"
    schema = response_format["json_schema"]["schema"]
    assert schema["properties"]["results"]["items"] == ITEM_SCHEMA


async def test_classify_batch_disables_thinking_on_the_wire():
    client, fake = _client([_payload("t0")])
    await client.classify_batch(_items(1), instructions=INSTRUCTIONS, item_schema=ITEM_SCHEMA)
    assert fake.calls[0]["extra_body"] == {"chat_template_kwargs": {"thinking": False}}


# --- finish_reason == "length" (defect 1c) --------------------------------


async def test_length_truncation_splits_the_batch_instead_of_retrying_it():
    truncated = FakeResponse("", finish_reason="length")
    client, fake = _client([truncated, _payload("t0", "t1"), _payload("t2", "t3")])

    batch = await client.classify_batch(
        _items(4), instructions=INSTRUCTIONS, item_schema=ITEM_SCHEMA
    )

    assert len(fake.calls) == 3
    # The two follow-ups carry half the items each — not the original full batch.
    first_retry = fake.calls[1]["messages"][-1]["content"]
    assert "t0" in first_retry and "t1" in first_retry
    assert '"t2"' not in first_retry and '"t3"' not in first_retry
    assert [r["item_id"] for r in batch.results] == ["t0", "t1", "t2", "t3"]
    assert batch.missing_ids == []
    # Tokens from all three calls are accounted for.
    assert batch.usage.tokens_in == 33


async def test_length_on_a_single_item_doubles_the_budget_not_the_prompt():
    truncated = FakeResponse("", finish_reason="length")
    client, fake = _client([truncated, _payload("t0")])

    batch = await client.classify_batch(
        _items(1), instructions=INSTRUCTIONS, item_schema=ITEM_SCHEMA, max_tokens=1000
    )

    assert len(fake.calls) == 2
    assert fake.calls[0]["max_tokens"] == 1000
    assert fake.calls[1]["max_tokens"] == 2000
    assert [r["item_id"] for r in batch.results] == ["t0"]


async def test_a_truncated_reply_is_never_parsed_as_an_answer():
    # Even when the truncated body LOOKS like partial JSON it is discarded.
    truncated = FakeResponse('{"results": [{"item_id": "t0", "cat', finish_reason="length")
    client, fake = _client([truncated, _payload("t0")])
    batch = await client.classify_batch(
        _items(1), instructions=INSTRUCTIONS, item_schema=ITEM_SCHEMA
    )
    assert batch.results[0]["item_id"] == "t0"
    assert len(fake.calls) == 2


# --- reasoning_content fallback (defect 1d) -------------------------------


async def test_json_schema_calls_never_fall_back_to_reasoning_content():
    response = FakeResponse(None)
    response.choices[0].message.reasoning_content = "I think the user wants..."
    provider = NvidiaProvider(
        api_key="nvapi-test",
        base_url="https://x/v1",
        default_model="m",
        client=FakeOpenAIClient([response]),
    )
    result = await provider.call_model("go", json_schema={"type": "object"})
    assert result.text == ""


async def test_free_text_calls_still_fall_back_to_reasoning_content():
    response = FakeResponse(None)
    response.choices[0].message.reasoning_content = "free-text thought"
    provider = NvidiaProvider(
        api_key="nvapi-test",
        base_url="https://x/v1",
        default_model="m",
        client=FakeOpenAIClient([response]),
    )
    assert (await provider.call_model("go")).text == "free-text thought"


async def test_disable_thinking_is_off_the_wire_by_default():
    provider_client = FakeOpenAIClient([FakeResponse("ok")])
    provider = NvidiaProvider(
        api_key="nvapi-test", base_url="https://x/v1", default_model="m",
        client=provider_client,
    )
    await provider.call_model("go")
    assert "extra_body" not in provider_client.calls[0]


# --- wall-clock deadline (defect 1e) --------------------------------------


async def test_a_slow_call_is_cut_off_at_the_configured_deadline():
    class SlowCompletions:
        def __init__(self):
            self.calls = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            await asyncio.sleep(5.0)  # far past the deadline
            return FakeResponse("too late")

    class SlowClient:
        def __init__(self):
            self.chat = type("Chat", (), {})()
            self.chat.completions = SlowCompletions()

    provider = NvidiaProvider(
        api_key="nvapi-test",
        base_url="https://x/v1",
        default_model="m",
        client=SlowClient(),
        timeout_s=0.05,
        max_retries=1,
    )
    started = time.monotonic()
    with pytest.raises(LLMError, match="after 1 attempts"):
        await provider.call_model("go")
    assert time.monotonic() - started < 2.0, "deadline did not bound wall-clock time"


# --- failed-batch spend (defect 5) ----------------------------------------


async def test_a_fully_failed_batch_reports_its_token_spend_on_the_error():
    client, _ = _client(["not json", "still not json"])
    with pytest.raises(LLMSchemaError) as excinfo:
        await client.classify_batch(
            _items(2), instructions=INSTRUCTIONS, item_schema=ITEM_SCHEMA
        )
    usage = excinfo.value.usage
    assert usage is not None
    assert usage.tokens_in == 22 and usage.tokens_out == 14  # two calls of (11, 7)
    assert usage.attempts == 2
