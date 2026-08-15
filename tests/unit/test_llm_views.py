"""Privacy boundary: ClassifierView makes an email body unrepresentable.

spec/architecture.md "Privacy Boundary": the ONLY fields ever serialized into an
LLM prompt are the ten allowed metadata fields. These tests assert the boundary
holds by construction — not by convention.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from llm.client import LLMClient, _build_batch_prompt
from llm.views import ALLOWED_PROMPT_FIELDS, SNIPPET_MAX_CHARS, ClassifierView

BODY_SENTINEL = "TOP-SECRET-BODY-CONTENT-9f8e7d"


def _view(**overrides) -> ClassifierView:
    base = dict(
        thread_id="t-1",
        sender_address="alerts@bank.com",
        sender_name="Bank Alerts",
        subject="New sign-in to your account",
        has_list_unsubscribe=True,
        reply_to="no-reply@bank.com",
        gmail_category="updates",
        thread_message_count=2,
        has_user_replied=False,
        snippet="A new device signed in to your account",
    )
    base.update(overrides)
    return ClassifierView(**base)


def test_field_set_is_exactly_the_allowed_prompt_fields():
    names = {f.name for f in dataclasses.fields(ClassifierView)}
    assert names == set(ALLOWED_PROMPT_FIELDS)
    assert "body" not in names


def test_a_body_kwarg_is_unrepresentable():
    with pytest.raises(TypeError):
        ClassifierView(thread_id="t-1", sender_address="a@b.c", body=BODY_SENTINEL)  # type: ignore[call-arg]


def test_frozen_and_slots_block_attribute_smuggling():
    view = _view()
    with pytest.raises((AttributeError, TypeError, dataclasses.FrozenInstanceError)):
        view.body = BODY_SENTINEL  # type: ignore[attr-defined]
    assert not hasattr(view, "__dict__")


def test_snippet_is_hard_truncated_to_90_chars():
    view = _view(snippet=BODY_SENTINEL + "x" * 500)
    assert len(view.snippet) == SNIPPET_MAX_CHARS


def test_thread_id_is_required_and_non_empty():
    with pytest.raises(ValueError):
        _view(thread_id="  ")


def test_to_prompt_dict_serializes_exactly_the_allowed_fields():
    payload = _view().to_prompt_dict()
    assert set(payload) == set(ALLOWED_PROMPT_FIELDS)
    # And is JSON-serializable as-is (the prompt builder json.dumps it).
    json.dumps(payload)


def test_built_prompt_never_contains_a_body_even_with_a_hostile_snippet():
    """A caller that pastes a whole body into `snippet` ships at most 90 chars,
    and no other field can carry it at all."""
    view = _view(snippet=BODY_SENTINEL[: SNIPPET_MAX_CHARS - 1])
    items = [view.to_prompt_dict()]
    prompt = _build_batch_prompt(
        items, "classify", {"type": "object"}, [view.thread_id]
    )
    parsed_items = json.loads(prompt[prompt.index("ITEMS:") + 6 : prompt.rindex("Reply with")])
    assert set(parsed_items[0]) == set(ALLOWED_PROMPT_FIELDS)


async def test_classify_batch_refuses_anything_that_is_not_a_classifier_view():
    """A raw dict — the only shape that could smuggle a `body` key — is rejected
    before any prompt is built or any provider is touched."""

    class _NeverCalled:
        name = "never"
        default_model = "never/model"

        async def call_model(self, *a, **k):  # pragma: no cover
            raise AssertionError("provider must not be reached")

    client = LLMClient(primary=_NeverCalled(), fallback=_NeverCalled())
    with pytest.raises(TypeError, match="ClassifierView"):
        await client.classify_batch(
            [{"thread_id": "t-1", "body": BODY_SENTINEL}],  # type: ignore[list-item]
            instructions="classify",
            item_schema={"type": "object"},
        )
