"""Shared OpenAI-compatible chat provider.

Both NVIDIA NIM and Gemini expose OpenAI-compatible chat-completions endpoints,
so one wrapper serves both, with:

* a **per-call swappable model id** (never hardcoded — the default comes from
  settings and any call may override it),
* a **hard wall-clock deadline per attempt** (``asyncio.wait_for``) — httpx read
  timeouts are per socket read, so a slowly-streaming model can run far past a
  plain float timeout; the outer deadline bounds total elapsed time. A stalled
  provider is impossible by construction (the old build hung on NVIDIA).
* bounded exponential-backoff retries (small — the failover client owns the
  bigger safety net: NVIDIA → Gemini),
* the process-wide outbound throttle (``llm.throttle``) on every request,
* token + cost accounting returned on every call.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import httpx
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    RateLimitError,
)

from llm import throttle
from llm.providers.base import LLMError, LLMResult, estimate_cost_usd
from observability.logging import get_logger

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_RETRIES = 2
CONNECT_TIMEOUT_S = 10.0
_RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class OpenAICompatProvider:
    """One OpenAI-compatible chat endpoint with hard timeouts and retries."""

    #: Human/provider name used in accounting rows and fallback events.
    provider_name = "openai-compat"
    #: Whether this endpoint understands Nemotron's ``chat_template_kwargs``.
    supports_thinking_toggle = False

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        default_model: str,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        client: Any | None = None,
        api_key_env_var: str = "API key",
    ) -> None:
        if not api_key:
            raise LLMError(
                f"{api_key_env_var} is not set. Add it to .env (see .env.example) "
                "before making LLM calls.",
                provider=self.provider_name,
            )
        if not default_model:
            raise LLMError(
                f"no default model configured for provider {self.provider_name!r}",
                provider=self.provider_name,
            )
        self._default_model = default_model
        self._timeout_s = float(timeout_s)
        self._max_retries = max(1, int(max_retries))
        self._log = get_logger(f"llm.{self.provider_name}")
        # `max_retries=0`: retries are owned here so backoff and logging are ours.
        # The httpx.Timeout caps connect/read/write/pool individually; the true
        # total-elapsed bound is the asyncio.wait_for deadline per attempt.
        self._client = client or AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=httpx.Timeout(self._timeout_s, connect=min(CONNECT_TIMEOUT_S, self._timeout_s)),
            max_retries=0,
        )

    @property
    def name(self) -> str:
        return self.provider_name

    @property
    def default_model(self) -> str:
        return self._default_model

    @property
    def timeout_s(self) -> float:
        return self._timeout_s

    # ------------------------------------------------------------------ hooks

    def _build_kwargs(
        self,
        *,
        model_id: str,
        messages: list[dict[str, str]],
        json_schema: dict[str, Any] | None,
        temperature: float,
        max_tokens: int,
        disable_thinking: bool,
    ) -> dict[str, Any]:
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
        if disable_thinking and self.supports_thinking_toggle:
            kwargs["extra_body"] = {"chat_template_kwargs": {"thinking": False}}
        return kwargs

    # ------------------------------------------------------------------ call

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
    ) -> LLMResult:
        """One chat completion. ``model`` overrides the configured default."""
        if not prompt or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")

        model_id = model or self._default_model
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        kwargs = self._build_kwargs(
            model_id=model_id,
            messages=messages,
            json_schema=json_schema,
            temperature=temperature,
            max_tokens=max_tokens,
            disable_thinking=disable_thinking,
        )

        started = time.monotonic()
        response, attempts = await self._request_with_retries(kwargs)
        latency_ms = int((time.monotonic() - started) * 1000)

        choice = response.choices[0]
        text = choice.message.content or ""
        if not text and json_schema is None:
            # Free-text calls only: reasoning models can put everything in
            # `reasoning_content`. A JSON-schema call must NEVER fall back to the
            # thought stream — chain-of-thought is not a parseable answer.
            text = getattr(choice.message, "reasoning_content", "") or ""
        usage = getattr(response, "usage", None)
        tokens_in = int(getattr(usage, "prompt_tokens", 0) or 0)
        tokens_out = int(getattr(usage, "completion_tokens", 0) or 0)

        return LLMResult(
            text=text,
            model=getattr(response, "model", None) or model_id,
            provider=self.provider_name,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            usd=estimate_cost_usd(model_id, tokens_in, tokens_out),
            attempts=attempts,
            finish_reason=getattr(choice, "finish_reason", None),
        )

    async def _request_with_retries(self, kwargs: dict[str, Any]) -> tuple[Any, int]:
        last_exc: Exception | None = None
        stripped_schema = False
        for attempt in range(1, self._max_retries + 1):
            try:
                # The process-wide bucket gates EVERY outbound request — first
                # attempts and retries alike. Waiting is not a failure and is
                # never counted as a retry.
                await throttle.acquire()
                # The wait_for deadline bounds total wall-clock per attempt.
                # httpx's read timeout alone is per socket read, so a model that
                # streams tokens slowly could otherwise run 2-3x past the budget
                # (observed: 215s calls against a 90s timeout on NVIDIA).
                response = await asyncio.wait_for(
                    self._client.chat.completions.create(**kwargs),
                    timeout=self._timeout_s,
                )
                return response, attempt
            except (TimeoutError, APITimeoutError, APIConnectionError, RateLimitError) as exc:
                last_exc = exc
            except APIStatusError as exc:
                if (
                    exc.status_code == 400
                    and "response_format" in kwargs
                    and not stripped_schema
                ):
                    # Some OpenAI-compat endpoints reject json_schema response
                    # formats. Drop it once and rely on the prompt + the client's
                    # schema validation — never on the endpoint's grammar.
                    kwargs = {k: v for k, v in kwargs.items() if k != "response_format"}
                    stripped_schema = True
                    last_exc = exc
                elif exc.status_code not in _RETRY_STATUS:
                    raise LLMError(
                        f"{self.provider_name} call failed ({exc.status_code}) for model "
                        f"{kwargs['model']!r}: {exc}",
                        model=kwargs.get("model"),
                        provider=self.provider_name,
                    ) from exc
                else:
                    last_exc = exc
            if attempt < self._max_retries:
                delay = min(2**attempt, 8) * (0.5 + random.random() / 2)
                # Never silent: a retried timeout must surface in the logs, not
                # as a stalled progress bar.
                self._log.warning(
                    "llm.retry",
                    provider=self.provider_name,
                    model=kwargs.get("model"),
                    attempt=attempt,
                    max_attempts=self._max_retries,
                    backoff_seconds=round(delay, 2),
                    cause=type(last_exc).__name__,
                )
                await asyncio.sleep(delay)
        raise LLMError(
            f"{self.provider_name} call failed after {self._max_retries} attempt(s) for "
            f"model {kwargs['model']!r}: {type(last_exc).__name__}: {last_exc}",
            model=kwargs.get("model"),
            provider=self.provider_name,
        ) from last_exc
