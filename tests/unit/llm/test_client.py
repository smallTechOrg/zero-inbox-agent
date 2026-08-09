"""LLMClient — batched structured classification, schema validation, degradation."""

from __future__ import annotations

import json

import pytest
from tests.unit.llm.fakes import FakeOpenAIClient, FakeResponse

from llm.client import LLMClient, get_llm_client, reset_llm_client
from llm.providers.base import LLMSchemaError
from llm.providers.nvidia import NvidiaProvider

ITEM_SCHEMA = {
    "type": "object",
    "required": ["item_id", "category", "action", "confidence", "reasoning"],
    "additionalProperties": True,
    "properties": {
        "item_id": {"type": "string"},
        "category": {"type": "string", "minLength": 1},
        "action": {"enum": ["keep", "archive", "unsure"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reasoning": {"type": "string", "minLength": 1},
        "time_sensitive": {"type": "boolean"},
    },
}

INSTRUCTIONS = "Classify each email thread as keep or archive."


def _items(n: int) -> list[dict]:
    return [
        {
            "item_id": f"t{i}",
            "from": f"sender{i}@example.com",
            "subject": f"Subject {i}",
            "snippet": "A short redacted snippet.",
        }
        for i in range(n)
    ]


def _result(item_id: str, **overrides) -> dict:
    base = {
        "item_id": item_id,
        "category": "newsletter",
        "action": "archive",
        "confidence": 0.9,
        "reasoning": "Bulk marketing mail from a list.",
        "time_sensitive": False,
    }
    base.update(overrides)
    return base


def _payload(*results: dict) -> str:
    return json.dumps({"results": list(results)})


def _client(script) -> tuple[LLMClient, FakeOpenAIClient]:
    fake = FakeOpenAIClient(script)
    provider = NvidiaProvider(
        api_key="nvapi-test",
        base_url="https://integrate.api.nvidia.com/v1",
        default_model="vendor/default-model",
        client=fake,
    )
    return LLMClient(provider), fake


# --- happy path ---------------------------------------------------------


async def test_classifies_a_whole_batch_in_one_call():
    items = _items(25)
    client, fake = _client([_payload(*[_result(i["item_id"]) for i in items])])

    batch = await client.classify_batch(
        items, instructions=INSTRUCTIONS, item_schema=ITEM_SCHEMA
    )

    assert len(fake.calls) == 1, "25 items must cost exactly one LLM call, not 25"
    assert len(batch.results) == 25
    assert [r["item_id"] for r in batch.results] == [i["item_id"] for i in items]
    assert batch.missing_ids == [] and batch.invalid == []
    assert batch.by_id["t7"]["action"] == "archive"
    assert batch.usage.tokens_in > 0 and batch.usage.attempts == 1
    assert batch.usage.usd >= 0.0
    # every item's id and evidence reached the prompt
    prompt = fake.calls[0]["messages"][-1]["content"]
    assert "t24" in prompt and "sender24@example.com" in prompt


async def test_model_is_swappable_per_batch_call():
    client, fake = _client([_payload(_result("t0"))])
    await client.classify_batch(
        _items(1),
        instructions=INSTRUCTIONS,
        item_schema=ITEM_SCHEMA,
        model="vendor/other-model",
    )
    assert fake.calls[0]["model"] == "vendor/other-model"


async def test_tolerates_markdown_fenced_and_preambled_json():
    body = _payload(_result("t0"))
    client, _ = _client([f"Sure! Here you go:\n```json\n{body}\n```"])
    batch = await client.classify_batch(
        _items(1), instructions=INSTRUCTIONS, item_schema=ITEM_SCHEMA
    )
    assert batch.results[0]["item_id"] == "t0"


async def test_accepts_a_bare_json_array_response():
    client, _ = _client([json.dumps([_result("t0"), _result("t1")])])
    batch = await client.classify_batch(
        _items(2), instructions=INSTRUCTIONS, item_schema=ITEM_SCHEMA
    )
    assert len(batch.results) == 2


# --- edge cases ---------------------------------------------------------


async def test_empty_batch_short_circuits_without_calling_the_model():
    client, fake = _client([])
    batch = await client.classify_batch(
        [], instructions=INSTRUCTIONS, item_schema=ITEM_SCHEMA
    )
    assert batch.results == [] and batch.missing_ids == [] and fake.calls == []


async def test_partial_response_triggers_one_retry_for_the_missing_items_only():
    items = _items(3)
    client, fake = _client(
        [_payload(_result("t0")), _payload(_result("t1"), _result("t2"))]
    )
    batch = await client.classify_batch(
        items, instructions=INSTRUCTIONS, item_schema=ITEM_SCHEMA
    )

    assert len(fake.calls) == 2
    retry_prompt = fake.calls[1]["messages"][-1]["content"]
    assert '["t1", "t2"]' in retry_prompt
    assert [r["item_id"] for r in batch.results] == ["t0", "t1", "t2"]
    assert batch.missing_ids == [] and batch.usage.attempts == 2


async def test_items_the_model_never_returns_are_reported_not_invented():
    client, _ = _client([_payload(_result("t0")), _payload()])
    batch = await client.classify_batch(
        _items(3), instructions=INSTRUCTIONS, item_schema=ITEM_SCHEMA
    )
    assert [r["item_id"] for r in batch.results] == ["t0"]
    assert batch.missing_ids == ["t1", "t2"]


async def test_schema_violating_and_hallucinated_results_are_rejected():
    client, _ = _client(
        [
            _payload(
                _result("t0", confidence=4.2),          # out of range
                _result("t1", action="delete"),          # not an allowed action
                _result("t2"),                           # valid
                _result("ghost"),                        # id was never sent
            ),
            _payload(),
        ]
    )
    batch = await client.classify_batch(
        _items(3), instructions=INSTRUCTIONS, item_schema=ITEM_SCHEMA
    )
    assert [r["item_id"] for r in batch.results] == ["t2"]
    assert sorted(batch.missing_ids) == ["t0", "t1"]
    assert len(batch.invalid) == 3


# --- error paths --------------------------------------------------------


async def test_unparsable_response_raises_a_schema_error_after_retries():
    client, fake = _client(["not json at all", "still not json"])
    with pytest.raises(LLMSchemaError):
        await client.classify_batch(
            _items(2), instructions=INSTRUCTIONS, item_schema=ITEM_SCHEMA
        )
    assert len(fake.calls) == 2


async def test_item_missing_its_id_field_is_rejected_before_the_call():
    client, fake = _client([])
    with pytest.raises(ValueError, match="item_id"):
        await client.classify_batch(
            [{"subject": "no id here"}],
            instructions=INSTRUCTIONS,
            item_schema=ITEM_SCHEMA,
        )
    assert fake.calls == []


async def test_duplicate_item_ids_are_rejected():
    client, _ = _client([])
    with pytest.raises(ValueError, match="duplicate"):
        await client.classify_batch(
            [{"item_id": "dup"}, {"item_id": "dup"}],
            instructions=INSTRUCTIONS,
            item_schema=ITEM_SCHEMA,
        )


async def test_empty_model_response_raises_schema_error():
    client, _ = _client([FakeResponse(""), FakeResponse("")])
    with pytest.raises(LLMSchemaError):
        await client.classify_batch(
            _items(1), instructions=INSTRUCTIONS, item_schema=ITEM_SCHEMA
        )


def test_call_model_sync_refuses_to_run_inside_an_event_loop():
    import asyncio

    client, _ = _client([FakeResponse("hi")])

    async def inner():
        with pytest.raises(RuntimeError, match="event loop"):
            client.call_model_sync("hi")

    asyncio.run(inner())


def test_get_llm_client_is_a_singleton_and_resettable(monkeypatch):
    monkeypatch.setenv("AGENT_NVIDIA_API_KEY", "nvapi-unit-test")
    monkeypatch.setenv("AGENT_NVIDIA_DEFAULT_MODEL", "vendor/default-model")
    import config.settings as settings_module

    settings_module._settings = None
    reset_llm_client()
    first = get_llm_client()
    assert get_llm_client() is first
    assert first.default_model == "vendor/default-model"
    reset_llm_client()
    assert get_llm_client() is not first
    reset_llm_client()
