"""Integration: real NVIDIA NIM and real Gemini, using keys from `.env`.

No mocks. These calls cost real tokens and prove the batched, schema-validated,
privacy-bounded classification path that the triage graph depends on — including
the automatic NVIDIA → Gemini failover with a deliberately broken NVIDIA key.
"""

from __future__ import annotations

import os

import pytest

from config.settings import get_settings
from llm.client import LLMClient, make_fallback_provider, make_primary_provider
from llm.providers.base import LLMError
from llm.providers.nvidia import NvidiaProvider
from llm.views import ClassifierView

pytestmark = pytest.mark.integration

BODY_SENTINEL = "TOP-SECRET-BODY-CONTENT-9f8e7d"

CLASSIFICATION_SCHEMA = {
    "type": "object",
    "required": ["thread_id", "category", "confidence", "reason"],
    "additionalProperties": True,
    "properties": {
        "thread_id": {"type": "string"},
        "category": {
            "enum": ["Finance", "Newsletters", "Notifications", "Personal", "Promotions"]
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string", "minLength": 1},
    },
}

INSTRUCTIONS = """You triage a Gmail inbox using ONLY headers and a short snippet.
For each thread decide:
- category: one of Finance, Newsletters, Notifications, Personal, Promotions
- confidence: 0..1
- reason: one short sentence naming the evidence you used
Never guess a thread_id."""

_SYNTHETIC = [
    ("This week in tech writing", "digest@substack.com", "Substack", "promotions", True, "Read the latest essays from your subs"),
    ("Your invoice INV-2031 is due Friday", "billing@vendor.com", "Vendor Billing", "updates", False, "Amount due 240.00 by Friday"),
    ("Re: dinner on Saturday?", "maya@personal.com", "Maya", "", False, "Works for me, 7pm?"),
    ("New sign-in to your account", "security@bank.com", "Bank Security", "updates", False, "A new device signed in"),
    ("50% off everything this weekend", "deals@shoestore.com", "Shoe Store", "promotions", True, "Flash sale ends Sunday"),
]


def _synthetic_views(count: int = 25) -> list[ClassifierView]:
    views = []
    for index in range(count):
        subject, sender, name, tab, unsub, snippet = _SYNTHETIC[index % len(_SYNTHETIC)]
        views.append(
            ClassifierView(
                thread_id=f"thread-{index:03d}",
                sender_address=sender,
                sender_name=name,
                subject=f"{subject} #{index}",
                has_list_unsubscribe=unsub,
                gmail_category=tab,
                thread_message_count=1 + (index % 3),
                has_user_replied=index % 5 == 2,
                snippet=snippet,
            )
        )
    return views


@pytest.fixture(scope="module")
def settings():
    s = get_settings()
    if not s.has_nvidia_key:
        pytest.fail("AGENT_NVIDIA_API_KEY is not set in .env — real-key tests cannot run.")
    if not os.environ.get("AGENT_GEMINI_API_KEY", "").strip() and not str(
        getattr(s, "gemini_api_key", "") or ""
    ).strip():
        pytest.fail("AGENT_GEMINI_API_KEY is not set in .env — fallback tests cannot run.")
    return s


@pytest.fixture(scope="module")
def live_client(settings) -> LLMClient:
    return LLMClient(make_primary_provider(settings), make_fallback_provider(settings))


# --- happy path: real NVIDIA ---------------------------------------------


async def test_batched_classification_of_25_views_against_real_nvidia(live_client):
    views = _synthetic_views(25)

    batch = await live_client.classify_batch(
        views,
        instructions=INSTRUCTIONS,
        item_schema=CLASSIFICATION_SCHEMA,
        max_attempts=3,
    )

    assert batch.missing_ids == [], f"model failed to classify: {batch.missing_ids}"
    assert len(batch.results) == 25, "one schema-conformant result per thread is required"
    assert [r["thread_id"] for r in batch.results] == [v.thread_id for v in views]
    for result in batch.results:
        assert 0.0 <= result["confidence"] <= 1.0
        assert result["reason"].strip()

    # NVIDIA served it — a healthy primary never touches Gemini
    assert batch.fallback_events == []
    assert all(c.provider == "nvidia" for c in batch.calls)

    # real token + cost + latency accounting came back
    assert batch.usage.tokens_in > 0 and batch.usage.tokens_out > 0
    assert batch.usage.usd >= 0.0
    assert batch.usage.latency_ms > 0

    # the model discriminates rather than labelling everything the same
    by_id = batch.by_id
    assert by_id["thread-001"]["category"] == "Finance", "an invoice is Finance"
    assert by_id["thread-002"]["category"] == "Personal", "a dinner reply is Personal"


# --- fallback: broken NVIDIA key → real Gemini serves the batch ----------


async def test_nvidia_failure_falls_over_to_real_gemini_and_reports_it(settings):
    broken_primary = NvidiaProvider(
        api_key="nvapi-deliberately-invalid-key-for-fallback-test",
        base_url=settings.nvidia_base_url,
        default_model=settings.nvidia_default_model,
        timeout_s=settings.llm_timeout_seconds,
        max_retries=1,
    )
    client = LLMClient(broken_primary, make_fallback_provider(settings))
    views = _synthetic_views(5)

    batch = await client.classify_batch(
        views, instructions=INSTRUCTIONS, item_schema=CLASSIFICATION_SCHEMA, max_attempts=3
    )

    assert len(batch.results) == 5
    assert batch.missing_ids == []
    assert batch.used_fallback is True
    event = batch.fallback_events[0]
    assert (event.from_provider, event.to_provider) == ("nvidia", "gemini")
    assert event.reason
    served = [c for c in batch.calls if c.provider == "gemini"]
    assert served and all(c.fallback for c in served)
    assert batch.usage.tokens_in > 0 and batch.usage.tokens_out > 0


# --- privacy: the real serialized payload carries no body -----------------


async def test_real_llm_payload_contains_no_body_for_a_sentinel_thread(live_client, monkeypatch):
    """A fixture thread whose 'body' holds a sentinel: the sentinel must not
    appear anywhere in the outbound prompt (ClassifierView cannot carry it)."""
    import llm.client as client_module

    captured: list[str] = []
    original = client_module._build_batch_prompt

    def _spy(items, instructions, item_schema, expected_ids):
        prompt = original(items, instructions, item_schema, expected_ids)
        captured.append(prompt)
        return prompt

    monkeypatch.setattr(client_module, "_build_batch_prompt", _spy)

    # The "email" has a body; the ClassifierView type cannot represent it.
    view = ClassifierView(
        thread_id="sentinel-1",
        sender_address="hr@example.com",
        subject="Offer letter",
        snippet="Please find attached",
    )
    batch = await live_client.classify_batch(
        [view], instructions=INSTRUCTIONS, item_schema=CLASSIFICATION_SCHEMA, max_attempts=3
    )
    assert captured, "the prompt builder must be the single path to the wire"
    assert all(BODY_SENTINEL not in prompt for prompt in captured)
    assert len(batch.results) == 1


# --- edge + error paths ---------------------------------------------------


async def test_single_view_batch_still_returns_one_valid_result(live_client):
    batch = await live_client.classify_batch(
        _synthetic_views(1),
        instructions=INSTRUCTIONS,
        item_schema=CLASSIFICATION_SCHEMA,
        max_attempts=3,
    )
    assert len(batch.results) == 1
    assert batch.results[0]["thread_id"] == "thread-000"
    assert batch.missing_ids == []


async def test_unknown_model_id_fails_fast_with_a_clear_error(settings):
    client = LLMClient(make_primary_provider(settings), _no_fallback=True)
    with pytest.raises(LLMError) as excinfo:
        await client.call_model(
            "hello", model="vendor/definitely-not-a-real-model", max_tokens=16
        )
    assert "vendor/definitely-not-a-real-model" in str(excinfo.value)
