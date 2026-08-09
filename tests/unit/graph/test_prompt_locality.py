"""Prompt-spy data-locality tests — what ACTUALLY leaves for the LLM.

A fake provider is spliced under the real ``LLMClient`` so the exact outgoing
prompt is captured at the wire boundary. Items are fed through the redaction
chokepoint carrying body text, oversized snippets, secrets and hostile extra
keys; the assertions prove the ``_thread_payload`` allowlist held:

- no body text ever reaches the prompt,
- the snippet is truncated to <=200 chars,
- secrets are redacted,
- unknown/extra item keys are dropped, not forwarded.

Covers both tier 3 (llm_classify_batch) and tier 4 (deep_read_escalation).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from graph import nodes
from llm.client import LLMClient
from llm.providers.base import LLMResult
from tools.rules import DEFAULT_TAXONOMY

BODY_MARKER = "ZZBODYMARKERZZ"
OVERFLOW_MARKER = "ZZOVERFLOWMARKERZZ"  # parked past the 200-char snippet budget
SECRET_KEY_STRING = "sk-abcdefghijklmnopqrstuvwx"
HIDDEN_NOTE = "TOPSECRET-INTERNAL-NOTE-91"


class SpyProvider:
    """Records every outgoing prompt + kwargs; replies with valid verdicts."""

    default_model = "spy/model"

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.kwargs: list[dict] = []

    async def call_model(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        json_schema: dict | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        disable_thinking: bool = False,
    ) -> LLMResult:
        self.prompts.append(prompt)
        self.kwargs.append(
            {"json_schema": json_schema, "disable_thinking": disable_thinking}
        )
        item_ids = [
            line.rsplit('"item_id": "', 1)[1].split('"', 1)[0]
            for line in prompt.splitlines()
            if '"item_id": "' in line
        ] or ["i1"]
        verdicts = [
            {
                "item_id": item_id,
                "category": "people",
                "action": "keep",
                "confidence": 0.9,
                "reasoning": "Human mail addressed to the user.",
            }
            for item_id in dict.fromkeys(item_ids)
        ]
        text = (
            json.dumps({"results": verdicts})
            if json_schema and "results" in json_schema.get("properties", {})
            else json.dumps(verdicts[0])
        )
        return LLMResult(
            text=text, model="spy/model", tokens_in=10, tokens_out=5, finish_reason="stop"
        )


def leaky_item(index: int = 1) -> dict:
    """A thread the way a hostile/naive adapter might hand it over."""
    return {
        "id": f"i{index}",
        "external_thread_id": f"t{index}",
        "subject": "Quick question",
        "from_name": "Person",
        "from_email": f"person{index}@example.com",
        "from_domain": "example.com",
        "list_id": None,
        "message_count": 1,
        "has_attachments": False,
        "is_unread": True,
        "internal_date": datetime.now(timezone.utc),
        # Body-like fields that must be dropped at the chokepoint.
        "body": f"Full private email body {BODY_MARKER} with my key {SECRET_KEY_STRING}",
        "body_text": f"also body {BODY_MARKER}",
        # Oversized snippet: secret early, overflow marker parked past 200 chars.
        "snippet": (
            f"Please use my api key {SECRET_KEY_STRING} for the deploy. "
            + "padding words " * 20
            + OVERFLOW_MARKER
        ),
        # Extra keys the allowlist must never forward.
        "internal_note": HIDDEN_NOTE,
        "raw_headers": {"X-Secret": HIDDEN_NOTE},
    }


@pytest.fixture
def spy(monkeypatch):
    provider = SpyProvider()
    client = LLMClient(provider)
    monkeypatch.setattr("llm.client.get_llm_client", lambda: client)
    return provider


def _state(**overrides) -> dict:
    state = {
        "run_id": "run-loc",
        "user_id": "user-loc",
        "channel_account_id": "acct-loc",
        "categories": DEFAULT_TAXONOMY,
        "rules": [],
        "sender_stats": {},
        "settings": {"confidence_floor": 0.75},
        "resolved": [],
        "llm_decisions": [],
        "deep_queue": [],
        "llm_calls": [],
    }
    state.update(overrides)
    return state


def _redacted_items(count: int = 2) -> list[dict]:
    """Items as the graph sees them: through the redact_items chokepoint."""
    out = nodes.redact_items(_state(items=[leaky_item(n + 1) for n in range(count)]))
    return out["items"]


def _assert_locality_held(prompt: str) -> None:
    assert BODY_MARKER not in prompt, "body text leaked into the LLM prompt"
    assert OVERFLOW_MARKER not in prompt, "snippet exceeded the 200-char budget"
    assert SECRET_KEY_STRING not in prompt, "an API key leaked unredacted"
    assert "[REDACTED:api_key]" in prompt, "the redaction placeholder is missing"
    assert HIDDEN_NOTE not in prompt, "an extra item key leaked past the allowlist"
    assert '"body"' not in prompt and '"body_text"' not in prompt


class TestTier3PromptLocality:
    def test_the_batch_prompt_carries_only_the_allowlisted_shape(self, spy):
        items = _redacted_items(2)
        out = nodes.llm_classify_batch(_state(batch=items))

        assert len(spy.prompts) == 1
        prompt = spy.prompts[0]
        _assert_locality_held(prompt)
        # The evidence that SHOULD be there is there.
        assert "person1@example.com" in prompt
        assert "Quick question" in prompt
        assert all(d["decided_by"] == "llm" for d in out["llm_decisions"])

    def test_every_snippet_in_the_prompt_is_within_budget(self, spy):
        nodes.llm_classify_batch(_state(batch=_redacted_items(2)))
        prompt = spy.prompts[0]
        # Parse the ITEMS payload back out of the prompt and measure each snippet.
        items_json = prompt.split("ITEMS:\n", 1)[1].rsplit("\n\nReply with", 1)[0]
        payloads = json.loads(items_json)
        assert payloads, "no item payloads found in the prompt"
        for payload in payloads:
            assert len(payload["snippet"]) <= 200
            assert set(payload) <= {
                "item_id", "from", "from_email", "domain", "list_id",
                "has_unsubscribe", "subject", "snippet", "messages", "unread",
                "attachments", "age_days",
            }

    def test_tier3_uses_structured_output_with_thinking_disabled(self, spy):
        nodes.llm_classify_batch(_state(batch=_redacted_items(1)))
        sent = spy.kwargs[0]
        assert sent["json_schema"] is not None
        assert sent["disable_thinking"] is True


class TestTier4PromptLocality:
    def test_the_deep_read_prompt_carries_only_the_allowlisted_shape(self, spy):
        items = _redacted_items(1)
        out = nodes.deep_read_escalation(_state(deep_queue=items))

        assert len(spy.prompts) == 1
        _assert_locality_held(spy.prompts[0])
        assert out["llm_decisions"][0]["decided_by"] == "llm_deep"

    def test_deep_read_uses_structured_output_with_thinking_disabled(self, spy):
        nodes.deep_read_escalation(_state(deep_queue=_redacted_items(1)))
        sent = spy.kwargs[0]
        assert sent["json_schema"] is not None
        assert sent["disable_thinking"] is True

    def test_sender_history_in_the_prompt_is_stats_not_content(self, spy):
        items = _redacted_items(1)
        state = _state(
            deep_queue=items,
            sender_stats={
                "person1@example.com": {
                    "received_count": 4,
                    "replied_count": 2,
                    "ever_replied": True,
                    "archived_by_user_count": 0,
                }
            },
        )
        nodes.deep_read_escalation(state)
        prompt = spy.prompts[0]
        assert '"ever_replied": true' in prompt
        _assert_locality_held(prompt)
