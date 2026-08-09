"""NVIDIA NIM provider — the only LLM provider in v1.

NIM exposes an OpenAI-compatible chat-completions API at
``https://integrate.api.nvidia.com/v1``, so this is a thin wrapper over the
``openai`` async SDK with:

* a **per-call swappable model id** (never hardcoded — the default comes from
  ``AGENT_NVIDIA_DEFAULT_MODEL`` and any call may override it),
* an explicit timeout and bounded exponential-backoff retries,
* token + cost accounting returned on every call.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    RateLimitError,
)

from llm.providers.base import LLMError, LLMResult, estimate_cost_usd

DEFAULT_TIMEOUT_S = 120.0
DEFAULT_MAX_RETRIES = 3
_RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class NvidiaProvider:
    """OpenAI-compatible NVIDIA NIM chat provider."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        default_model: str,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        client: Any | None = None,
    ) -> None:
        if not api_key:
            raise LLMError(
                "AGENT_NVIDIA_API_KEY is not set. Add it to .env "
                "(see .env.example) before making LLM calls."
            )
        if not default_model:
            raise LLMError("AGENT_NVIDIA_DEFAULT_MODEL is not set.")
        self._default_model = default_model
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        # `max_retries=0`: retries are owned here so backoff and logging are ours.
        self._client = client or AsyncOpenAI(
            api_key=api_key, base_url=base_url, timeout=timeout_s, max_retries=0
        )

    @property
    def default_model(self) -> str:
        return self._default_model

    async def call_model(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        json_schema: dict[str, Any] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> LLMResult:
        """One chat completion. ``model`` overrides the configured default."""
        if not prompt or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")

        model_id = model or self._default_model
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        kwargs: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": json_schema, "strict": True},
            }

        started = time.monotonic()
        response, attempts = await self._request_with_retries(kwargs)
        latency_ms = int((time.monotonic() - started) * 1000)

        choice = response.choices[0]
        text = choice.message.content or ""
        if not text:
            # Reasoning models can put everything in `reasoning_content`.
            text = getattr(choice.message, "reasoning_content", "") or ""
        usage = getattr(response, "usage", None)
        tokens_in = int(getattr(usage, "prompt_tokens", 0) or 0)
        tokens_out = int(getattr(usage, "completion_tokens", 0) or 0)

        return LLMResult(
            text=text,
            model=getattr(response, "model", None) or model_id,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            usd=estimate_cost_usd(model_id, tokens_in, tokens_out),
            attempts=attempts,
            finish_reason=getattr(choice, "finish_reason", None),
        )

    async def _request_with_retries(self, kwargs: dict[str, Any]) -> tuple[Any, int]:
        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                return await self._client.chat.completions.create(**kwargs), attempt
            except (APITimeoutError, APIConnectionError, RateLimitError) as exc:
                last_exc = exc
            except APIStatusError as exc:
                if exc.status_code not in _RETRY_STATUS:
                    raise LLMError(
                        f"NVIDIA NIM call failed ({exc.status_code}) for model "
                        f"{kwargs['model']!r}: {exc}"
                    ) from exc
                last_exc = exc
            if attempt < self._max_retries:
                await asyncio.sleep(min(2**attempt, 8) * (0.5 + random.random() / 2))
        raise LLMError(
            f"NVIDIA NIM call failed after {self._max_retries} attempts for model "
            f"{kwargs['model']!r}: {last_exc}"
        ) from last_exc
