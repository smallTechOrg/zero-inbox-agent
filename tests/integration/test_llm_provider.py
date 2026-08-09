"""Integration: the real NVIDIA NIM endpoint, using the key from `.env`.

No mocks. These calls cost real tokens and prove the batched, schema-validated
classification path that tier 3 of the triage cascade depends on.
"""

from __future__ import annotations

import pytest

from config.settings import get_settings
from llm.client import LLMClient
from llm.providers.base import LLMError

pytestmark = pytest.mark.integration

CLASSIFICATION_SCHEMA = {
    "type": "object",
    "required": ["item_id", "category", "action", "confidence", "reasoning"],
    "additionalProperties": True,
    "properties": {
        "item_id": {"type": "string"},
        "category": {
            "enum": ["newsletter", "notification", "personal", "transactional", "promotion"]
        },
        "action": {"enum": ["keep", "archive", "unsure"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reasoning": {"type": "string", "minLength": 1},
        "time_sensitive": {"type": "boolean"},
    },
}

INSTRUCTIONS = """You triage a Gmail inbox. For each email thread decide:
- category: one of newsletter, notification, personal, transactional, promotion
- action: "keep" if the user would want it in their inbox, "archive" if it is noise,
  "unsure" if you genuinely cannot tell from the headers alone
- confidence: 0..1
- reasoning: one short sentence naming the evidence you used
- time_sensitive: true for deadlines, invoices, legal or security alerts
Never guess an id. Only headers and a redacted snippet are available."""

_SYNTHETIC = [
    ("Weekly Substack digest", "digest@substack.com", "This week in tech writing"),
    ("Your invoice INV-2031 is due Friday", "billing@vendor.com", "Amount due 240.00"),
    ("Re: dinner on Saturday?", "maya@personal.com", "Works for me, 7pm?"),
    ("New sign-in to your account", "security@bank.com", "A new device signed in"),
    ("50% off everything this weekend", "deals@shoestore.com", "Flash sale ends Sunday"),
]


def _synthetic_items(count: int = 25) -> list[dict]:
    items = []
    for index in range(count):
        subject, sender, snippet = _SYNTHETIC[index % len(_SYNTHETIC)]
        items.append(
            {
                "item_id": f"thread-{index:03d}",
                "from": sender,
                "subject": f"{subject} #{index}",
                "list_id": "<list.substack.com>" if "substack" in sender else "",
                "snippet": snippet,
            }
        )
    return items


@pytest.fixture(scope="module")
def live_client() -> LLMClient:
    settings = get_settings()
    if not settings.has_nvidia_key:
        pytest.fail("AGENT_NVIDIA_API_KEY is not set in .env — real-key tests cannot run.")
    return LLMClient()


# --- happy path ---------------------------------------------------------


@pytest.mark.asyncio
async def test_batched_classification_of_25_items_against_real_nim(live_client):
    items = _synthetic_items(25)

    batch = await live_client.classify_batch(
        items,
        instructions=INSTRUCTIONS,
        item_schema=CLASSIFICATION_SCHEMA,
        max_attempts=3,
    )

    assert batch.missing_ids == [], f"model failed to classify: {batch.missing_ids}"
    assert len(batch.results) == 25, "one schema-conformant result per item is required"
    assert [r["item_id"] for r in batch.results] == [i["item_id"] for i in items]

    for result in batch.results:
        assert result["action"] in {"keep", "archive", "unsure"}
        assert 0.0 <= result["confidence"] <= 1.0
        assert result["reasoning"].strip()

    # real token + cost accounting came back
    assert batch.usage.tokens_in > 0 and batch.usage.tokens_out > 0
    assert batch.usage.usd >= 0.0
    assert batch.usage.latency_ms > 0
    assert batch.usage.model

    # the model actually discriminates rather than labelling everything the same
    by_id = batch.by_id
    assert by_id["thread-001"]["action"] in {"keep", "unsure"}, "an unpaid invoice is not noise"
    assert by_id["thread-002"]["action"] in {"keep", "unsure"}, "a personal reply is not noise"
    assert by_id["thread-001"]["time_sensitive"] is True


@pytest.mark.asyncio
async def test_single_call_returns_text_and_respects_a_per_call_model_override(live_client):
    settings = get_settings()
    result = await live_client.call_model(
        "Reply with exactly one word: PONG",
        system="You answer in a single word.",
        model=settings.nvidia_default_model,
        max_tokens=64,
    )
    assert "pong" in result.text.lower()
    assert result.tokens_in > 0
    assert result.latency_ms > 0


# --- edge case ----------------------------------------------------------


@pytest.mark.asyncio
async def test_single_item_batch_still_returns_one_valid_result(live_client):
    batch = await live_client.classify_batch(
        _synthetic_items(1),
        instructions=INSTRUCTIONS,
        item_schema=CLASSIFICATION_SCHEMA,
    )
    assert len(batch.results) == 1
    assert batch.results[0]["item_id"] == "thread-000"
    assert batch.missing_ids == []


# --- error path ---------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_model_id_fails_fast_with_a_clear_error(live_client):
    with pytest.raises(LLMError) as excinfo:
        await live_client.call_model(
            "hello", model="vendor/definitely-not-a-real-model", max_tokens=16
        )
    assert "vendor/definitely-not-a-real-model" in str(excinfo.value)
