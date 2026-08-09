"""Provider-layer contract for the LLM layer.

Every provider returns an :class:`LLMResult` so the caller always gets token and
cost accounting alongside the text — see spec/architecture.md "Provider layer contract".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# USD per 1M tokens, keyed by model id. NVIDIA NIM's hosted preview endpoints are
# not metered per token today, so unknown models price at 0.0 rather than guessing.
# Phase 3 (`src/llm/models_catalog.py`) replaces this with the user-facing catalogue.
PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    # model id: (input, output)
    "nvidia/nemotron-3-nano-30b-a3b": (0.0, 0.0),
}

_UNKNOWN_MODEL_PRICE = (0.0, 0.0)


def estimate_cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    """USD cost for a call. Unknown models cost 0.0 — never raise on pricing."""
    price_in, price_out = PRICING_USD_PER_MTOK.get(model, _UNKNOWN_MODEL_PRICE)
    return (tokens_in * price_in + tokens_out * price_out) / 1_000_000


@dataclass(frozen=True)
class LLMResult:
    """One completed model call: its text plus full accounting."""

    text: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    usd: float = 0.0
    attempts: int = 1
    finish_reason: str | None = None

    @property
    def tokens_total(self) -> int:
        return self.tokens_in + self.tokens_out


@dataclass(frozen=True)
class BatchClassification:
    """Result of one batched structured-classification call.

    ``results`` holds only schema-conformant objects. ``missing_ids`` names the
    input items the model failed to classify — the caller degrades those to
    ``needs_your_call`` rather than guessing (see spec/capabilities/cost-tiered-triage.md).
    """

    results: list[dict[str, Any]] = field(default_factory=list)
    missing_ids: list[str] = field(default_factory=list)
    invalid: list[dict[str, Any]] = field(default_factory=list)
    usage: LLMResult | None = None
    id_field: str = "item_id"

    @property
    def by_id(self) -> dict[str, dict[str, Any]]:
        return {str(r[self.id_field]): r for r in self.results if self.id_field in r}


class LLMError(RuntimeError):
    """Any unrecoverable failure of the LLM layer after retries are exhausted."""


class LLMSchemaError(LLMError):
    """The model's response could not be parsed/validated against the schema."""


@runtime_checkable
class LLMProvider(Protocol):
    """Async, OpenAI-compatible chat provider with a per-call swappable model id."""

    @property
    def default_model(self) -> str: ...

    async def call_model(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        json_schema: dict[str, Any] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> LLMResult: ...
