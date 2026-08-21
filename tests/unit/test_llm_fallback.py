"""Failover semantics: NVIDIA primary → automatic Gemini fallback, per batch.

No network. Fake providers script the failure modes; the contract under test is
the one spec/architecture.md gives `src/llm/`: fail over on error/429/timeout,
surface fallback events + full accounting, and prefer returning to the primary
on the next batch.
"""

from __future__ import annotations

import json

import pytest

from llm.client import LLMClient
from llm.providers.base import LLMError, LLMResult
from llm.views import ClassifierView

SCHEMA = {
    "type": "object",
    "required": ["thread_id", "category", "confidence", "reason"],
    "properties": {
        "thread_id": {"type": "string"},
        "category": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
    },
    "additionalProperties": True,
}


def _views(count: int) -> list[ClassifierView]:
    return [
        ClassifierView(
            thread_id=f"t-{i}",
            sender_address=f"sender{i}@example.com",
            subject=f"Subject {i}",
            snippet="hello",
        )
        for i in range(count)
    ]


def _answer(views) -> str:
    return json.dumps(
        {
            "results": [
                {
                    "thread_id": v.thread_id,
                    "category": "Newsletters",
                    "confidence": 0.9,
                    "reason": "looks like a newsletter",
                }
                for v in views
            ]
        }
    )


class FakeProvider:
    """Scripted provider: each entry is a response text or an exception."""

    def __init__(self, name: str, script: list):
        self.provider_name = name
        self._script = list(script)
        self.calls: list[dict] = []

    @property
    def name(self) -> str:
        return self.provider_name

    @property
    def default_model(self) -> str:
        return f"{self.provider_name}/model"

    async def call_model(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        return LLMResult(
            text=step,
            model=kwargs.get("model") or self.default_model,
            provider=self.provider_name,
            tokens_in=100,
            tokens_out=50,
            latency_ms=12,
            usd=0.001,
        )


async def test_healthy_primary_never_touches_the_fallback():
    views = _views(3)
    primary = FakeProvider("nvidia", [_answer(views)])
    fallback = FakeProvider("gemini", [])
    batch = await LLMClient(primary=primary, fallback=fallback).classify_batch(
        views, instructions="classify", item_schema=SCHEMA
    )
    assert [r["thread_id"] for r in batch.results] == ["t-0", "t-1", "t-2"]
    assert batch.fallback_events == []
    assert fallback.calls == []
    assert batch.usage.fallback is False
    assert all(c.provider == "nvidia" for c in batch.calls)


async def test_primary_failure_fails_over_to_gemini_and_surfaces_the_event():
    views = _views(2)
    primary = FakeProvider("nvidia", [LLMError("nvidia timed out after 2 attempt(s)")])
    fallback = FakeProvider("gemini", [_answer(views)])
    batch = await LLMClient(primary=primary, fallback=fallback).classify_batch(
        views, instructions="classify", item_schema=SCHEMA
    )
    assert len(batch.results) == 2
    assert batch.missing_ids == []
    assert len(batch.fallback_events) == 1
    event = batch.fallback_events[0]
    assert (event.from_provider, event.to_provider) == ("nvidia", "gemini")
    assert event.to_model == "gemini/model"
    assert "timed out" in event.reason
    # accounting: the serving call is flagged as fallback
    assert batch.usage.fallback is True
    assert batch.calls[-1].provider == "gemini"
    assert batch.calls[-1].fallback is True


async def test_next_batch_prefers_nvidia_again_after_a_fallback_batch():
    """Failover is per batch, never sticky: batch 1 falls to Gemini, batch 2
    is served by a recovered NVIDIA without Gemini being called."""
    views = _views(1)
    primary = FakeProvider("nvidia", [LLMError("boom"), _answer(views)])
    fallback = FakeProvider("gemini", [_answer(views)])
    client = LLMClient(primary=primary, fallback=fallback)

    first = await client.classify_batch(views, instructions="c", item_schema=SCHEMA)
    assert first.used_fallback is True

    second = await client.classify_batch(views, instructions="c", item_schema=SCHEMA)
    assert second.used_fallback is False
    assert second.calls[0].provider == "nvidia"
    assert len(fallback.calls) == 1  # only the first batch


async def test_both_providers_failing_raises_a_clear_error_naming_both():
    views = _views(1)
    primary = FakeProvider("nvidia", [LLMError("nvidia down")])
    fallback = FakeProvider("gemini", [LLMError("gemini down")])
    with pytest.raises(LLMError) as excinfo:
        await LLMClient(primary=primary, fallback=fallback).classify_batch(
            views, instructions="c", item_schema=SCHEMA
        )
    message = str(excinfo.value)
    assert "nvidia" in message and "gemini" in message


async def test_no_fallback_configured_reraises_the_primary_error():
    views = _views(1)
    primary = FakeProvider("nvidia", [LLMError("nvidia down")])
    client = LLMClient(primary=primary, _no_fallback=True)
    assert client.has_fallback is False
    with pytest.raises(LLMError, match="nvidia down"):
        await client.classify_batch(views, instructions="c", item_schema=SCHEMA)


async def test_fallback_ignores_the_primary_model_override():
    """A per-call `model=` names a PRIMARY model; Gemini uses its own model."""
    primary = FakeProvider("nvidia", [LLMError("down")])
    fallback = FakeProvider("gemini", ["PONG"])
    result = await LLMClient(primary=primary, fallback=fallback).call_model(
        "ping", model="nvidia/some-experimental-model"
    )
    assert result.provider == "gemini"
    assert fallback.calls[0]["model"] == "gemini/model"
    assert result.fallback is True


async def test_empty_batch_returns_an_empty_result_without_any_call():
    primary = FakeProvider("nvidia", [])
    batch = await LLMClient(primary=primary, _no_fallback=True).classify_batch(
        [], instructions="c", item_schema=SCHEMA
    )
    assert batch.results == [] and batch.missing_ids == []
    assert primary.calls == []


async def test_malformed_entries_degrade_to_missing_or_invalid_never_a_crash():
    views = _views(2)
    text = json.dumps(
        {
            "results": [
                {"thread_id": "t-0", "category": "Finance", "confidence": 0.8, "reason": "ok"},
                {"thread_id": "t-1", "category": "Finance", "confidence": 7},  # invalid
            ]
        }
    )
    primary = FakeProvider("nvidia", [text, text])  # retry replays the same flaw
    batch = await LLMClient(primary=primary, _no_fallback=True).classify_batch(
        views, instructions="c", item_schema=SCHEMA, max_attempts=2
    )
    assert [r["thread_id"] for r in batch.results] == ["t-0"]
    assert batch.missing_ids == ["t-1"]
    assert batch.invalid  # the malformed entry is reported, not guessed
