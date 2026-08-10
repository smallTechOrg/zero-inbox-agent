"""POST /api/categories/propose — LLM-proposed taxonomy improvements."""

from __future__ import annotations

import json

import pytest

from llm.providers.base import LLMResult


class FakeLLMClient:
    """Minimal stand-in for LLMClient that returns a canned proposals array."""

    def __init__(self, proposals: list[dict] | None = None, raw_text: str | None = None):
        self._proposals = proposals or []
        self._raw_text = raw_text
        self.calls: list[dict] = []

    def call_model_sync(self, prompt: str, **kwargs) -> LLMResult:
        self.calls.append({"prompt": prompt, **kwargs})
        text = self._raw_text if self._raw_text is not None else json.dumps(self._proposals)
        return LLMResult(
            text=text,
            model="nvidia/test-model",
            tokens_in=100,
            tokens_out=50,
        )


@pytest.fixture
def fake_llm(monkeypatch):
    client = FakeLLMClient(
        proposals=[
            {
                "action": "add",
                "key": "receipts",
                "name": "Receipts",
                "description": "Purchase and order confirmations.",
                "reasoning": "Many recent emails are purchase receipts with no category.",
            }
        ]
    )
    # The endpoint does `from llm.client import get_llm_client` inside the function
    # body, so patching `llm.client.get_llm_client` is the right seam — each call
    # re-imports from the module, picking up the monkeypatched reference.
    import llm.client as llm_client_module

    monkeypatch.setattr(llm_client_module, "get_llm_client", lambda: client)
    return client


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_propose_taxonomy_returns_proposals_list(client, seed, sign_in, fake_llm):
    """Happy path: LLM returns a valid proposals array; endpoint surfaces it."""
    sign_in("user-alice")
    res = client.post("/api/categories/propose")

    assert res.status_code == 200
    data = res.json()["data"]
    assert isinstance(data["proposals"], list)
    assert len(data["proposals"]) == 1
    assert data["proposals"][0]["action"] == "add"
    assert data["proposals"][0]["key"] == "receipts"
    assert "model" in data
    assert "tokens" in data
    assert data["tokens"] == 150  # 100 in + 50 out


def test_propose_taxonomy_includes_current_categories_in_prompt(client, seed, sign_in, fake_llm):
    """The prompt sent to the LLM must include the user's existing taxonomy."""
    sign_in("user-alice")
    client.post("/api/categories/propose")

    assert len(fake_llm.calls) == 1
    prompt = fake_llm.calls[0]["prompt"]
    # Alice's seeded category key / name should appear
    assert "newsletters" in prompt.lower() or "Newsletters" in prompt


# ---------------------------------------------------------------------------
# Edge case: no decisions yet
# ---------------------------------------------------------------------------


def test_propose_taxonomy_with_no_decisions_still_returns_proposals(client, sign_in, fake_llm):
    """A brand-new user with no decision history gets a valid (possibly empty) response."""
    from db.models import User, UserSettings

    # Need a user with no seeded data — use a fresh client fixture rather than
    # relying on seed, so we control that there are zero decisions.
    # The existing `client` fixture is already wired to the isolated DB.
    sign_in("user-alice")
    # alice has no decisions in this test because seed was NOT requested
    res = client.post("/api/categories/propose")
    assert res.status_code == 200
    data = res.json()["data"]
    assert "proposals" in data


# ---------------------------------------------------------------------------
# Error path: LLM returns non-JSON
# ---------------------------------------------------------------------------


def test_propose_taxonomy_degrades_gracefully_when_llm_returns_garbage(
    client, seed, sign_in, monkeypatch
):
    """If the LLM returns un-parseable text, the endpoint returns an empty
    proposals list rather than a 500."""
    garbage_client = FakeLLMClient(raw_text="Sorry, I cannot help with that.")
    import llm.client as llm_client_module

    monkeypatch.setattr(llm_client_module, "get_llm_client", lambda: garbage_client)

    sign_in("user-alice")
    res = client.post("/api/categories/propose")
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["proposals"] == []


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------


def test_propose_taxonomy_requires_a_session(client):
    """Unauthenticated request must be rejected with 401."""
    res = client.post("/api/categories/propose")
    assert res.status_code == 401


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


def test_propose_taxonomy_is_scoped_per_user(client, seed, sign_in, fake_llm):
    """Bob's categories/decisions must not appear in Alice's proposal prompt."""
    sign_in("user-alice")
    client.post("/api/categories/propose")

    prompt = fake_llm.calls[0]["prompt"]
    # Bob's category id should not leak into Alice's prompt
    assert "user-bob" not in prompt
    assert "bob@" not in prompt
