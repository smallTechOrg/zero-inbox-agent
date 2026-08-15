"""Provider-layer contract for the LLM layer.

Every provider returns an :class:`LLMResult` so the caller always gets token,
cost, and latency accounting alongside the text. The failover client
(``llm.client``) additionally surfaces :class:`FallbackEvent`s and a per-call
list on :class:`BatchClassification` so callers can persist ``llm_calls`` rows
and stream fallback events to the feed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# USD per 1M tokens, keyed by model id. NVIDIA NIM's hosted endpoints are not
# metered per token today, so they price at 0.0. Gemini prices are the published
# 2.5 Flash-Lite rates. Unknown models price at 0.0 rather than guessing.
PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    # model id: (input, output)
    "nvidia/nemotron-3-nano-30b-a3b": (0.0, 0.0),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-flash-lite-latest": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-flash-latest": (0.30, 2.50),
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
    provider: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    usd: float = 0.0
    attempts: int = 1
    finish_reason: str | None = None
    fallback: bool = False  # True when this call was served by the fallback provider

    @property
    def tokens_total(self) -> int:
        return self.tokens_in + self.tokens_out


@dataclass(frozen=True)
class FallbackEvent:
    """One automatic provider failover, surfaced to callers for the feed/ledger."""

    from_provider: str
    from_model: str
    to_provider: str
    to_model: str
    reason: str
    at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class BatchClassification:
    """Result of one batched structured-classification call.

    ``results`` holds only schema-conformant objects. ``missing_ids`` names the
    input items the model failed to classify — the caller degrades those to
    needs-review rather than guessing. ``calls`` carries per-provider-call
    accounting (provider, model, tokens, latency, usd, fallback flag) for
    ``llm_calls`` persistence; ``fallback_events`` carries every automatic
    NVIDIA→Gemini failover that happened while serving this batch.
    """

    results: list[dict[str, Any]] = field(default_factory=list)
    missing_ids: list[str] = field(default_factory=list)
    invalid: list[dict[str, Any]] = field(default_factory=list)
    usage: LLMResult | None = None
    calls: list[LLMResult] = field(default_factory=list)
    fallback_events: list[FallbackEvent] = field(default_factory=list)
    id_field: str = "thread_id"

    @property
    def by_id(self) -> dict[str, dict[str, Any]]:
        return {str(r[self.id_field]): r for r in self.results if self.id_field in r}

    @property
    def used_fallback(self) -> bool:
        return bool(self.fallback_events)


class LLMError(RuntimeError):
    """Any unrecoverable failure of the LLM layer after retries are exhausted.

    ``usage`` (when present) carries the token/cost accounting accumulated before
    the failure so the caller can persist it — tokens spent on a failed batch are
    still real spend and belong in the audit trail. ``model``/``provider`` name
    what failed so the caller can report it in plain English.
    """

    def __init__(
        self,
        message: str = "",
        *,
        usage: "LLMResult | None" = None,
        model: str | None = None,
        provider: str | None = None,
    ) -> None:
        super().__init__(message)
        self.usage = usage
        self.model = model
        self.provider = provider


class LLMSchemaError(LLMError):
    """The model's response could not be parsed/validated against the schema."""


@runtime_checkable
class LLMProvider(Protocol):
    """Async, OpenAI-compatible chat provider with a per-call swappable model id."""

    @property
    def name(self) -> str: ...

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
        disable_thinking: bool = False,
    ) -> LLMResult: ...
