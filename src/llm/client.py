"""The LLM layer's public surface: one ``classify_batch()`` client.

Everything in the app talks to :class:`LLMClient`; nothing imports a provider
directly. Semantics (spec/architecture.md `src/llm/`):

* **Primary NVIDIA NIM, automatic Gemini fallback.** On error, HTTP 429, or
  timeout the current batch fails over to Gemini; the NEXT batch tries NVIDIA
  first again (prefer returning to primary — there is no sticky failover state).
* **Hard per-call timeout** (``AGENT_LLM_TIMEOUT_SECONDS``): a stalled provider
  is impossible by construction (``asyncio.wait_for`` in the provider layer).
* **Full accounting surfaced to callers**: every underlying provider call comes
  back on ``BatchClassification.calls`` (provider, model, tokens, latency, usd,
  fallback flag) and every failover on ``fallback_events`` — the caller persists
  ``llm_calls`` rows and emits feed events from these.
* **Privacy by type**: ``classify_batch`` accepts ONLY ``ClassifierView``
  instances — an email body is unrepresentable in the input type.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import time
from typing import Any, Mapping, Sequence

from jsonschema import Draft202012Validator

from config.settings import get_settings
from llm.providers.base import (
    BatchClassification,
    FallbackEvent,
    LLMError,
    LLMProvider,
    LLMResult,
    LLMSchemaError,
    estimate_cost_usd,
)
from llm.providers.gemini import GeminiProvider
from llm.providers.nvidia import NvidiaProvider
from llm.views import ClassifierView

__all__ = [
    "LLMClient",
    "get_llm_client",
    "reset_llm_client",
    "make_primary_provider",
    "make_fallback_provider",
    "BatchClassification",
    "ClassifierView",
    "FallbackEvent",
    "LLMResult",
    "LLMError",
    "LLMSchemaError",
]

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _log() -> Any:
    """Structured logger; bridged to the user's SSE feed by the observability
    layer so a single LLM call over a whole batch is never a silent minute."""
    from observability.logging import get_logger

    return get_logger("zero_inbox.llm")


_BATCH_SYSTEM = (
    "You are a precise classification engine. You classify a batch of items in a "
    "single pass and reply with JSON only — no prose, no markdown fences, no "
    "explanation outside the JSON."
)

_ID_FIELD = "thread_id"


def _setting(settings: Any, name: str, env_var: str, default: str = "") -> str:
    """Read a spec'd setting, tolerating the settings slice landing in parallel."""
    value = str(getattr(settings, name, "") or "").strip()
    return value or os.environ.get(env_var, "").strip() or default


def make_primary_provider(settings: Any | None = None) -> NvidiaProvider:
    """The primary provider: NVIDIA NIM."""
    s = settings or get_settings()
    return NvidiaProvider(
        api_key=s.nvidia_api_key,
        base_url=s.nvidia_base_url,
        default_model=s.nvidia_default_model,
        timeout_s=s.llm_timeout_seconds,
        max_retries=min(int(getattr(s, "llm_max_retries", 2) or 2), 2),
    )


def make_fallback_provider(settings: Any | None = None) -> GeminiProvider | None:
    """The automatic fallback: Gemini. ``None`` when no key is configured."""
    s = settings or get_settings()
    api_key = _setting(s, "gemini_api_key", "AGENT_GEMINI_API_KEY")
    if not api_key:
        return None
    # Spec names gemini-2.5-flash-lite, but Google has retired 2.x model ids for
    # new API keys (404 "no longer available to new users", verified live
    # 2026-08-15). `gemini-flash-lite-latest` is Google's stable alias for the
    # current flash-lite generation — same tier, same intent.
    model = _setting(
        s, "gemini_fallback_model", "AGENT_GEMINI_FALLBACK_MODEL", "gemini-flash-lite-latest"
    )
    return GeminiProvider(
        api_key=api_key,
        default_model=model,
        timeout_s=s.llm_timeout_seconds,
        max_retries=min(int(getattr(s, "llm_max_retries", 2) or 2), 2),
    )


class LLMClient:
    """NVIDIA-primary, Gemini-fallback client with per-batch failover."""

    def __init__(
        self,
        primary: LLMProvider | None = None,
        fallback: LLMProvider | None = None,
        *,
        default_model: str | None = None,
        _no_fallback: bool = False,
    ) -> None:
        self._primary = primary if primary is not None else make_primary_provider()
        if fallback is not None:
            self._fallback: LLMProvider | None = fallback
        elif _no_fallback:
            self._fallback = None
        else:
            self._fallback = make_fallback_provider()
        self._default_model = default_model or self._primary.default_model

    @property
    def default_model(self) -> str:
        return self._default_model

    @property
    def has_fallback(self) -> bool:
        return self._fallback is not None

    # ------------------------------------------------------------- failover

    async def _call_with_failover(
        self, prompt: str, *, model: str | None, **kwargs: Any
    ) -> tuple[LLMResult, FallbackEvent | None]:
        """One completion: primary first, Gemini on any primary failure.

        Every entry point starts here at the PRIMARY — a batch that fell over to
        Gemini does not stick; the next batch probes NVIDIA again automatically.
        """
        log = _log()
        model_id = model or self._default_model
        started = time.perf_counter()
        log.info("llm.call_started", provider=self._primary.name, model=model_id)
        try:
            result = await self._primary.call_model(prompt, model=model_id, **kwargs)
        except LLMError as primary_exc:
            log.warning(
                "llm.primary_failed",
                provider=self._primary.name,
                model=model_id,
                latency_ms=int((time.perf_counter() - started) * 1000),
                error=str(primary_exc)[:300],
            )
            if self._fallback is None:
                raise
            event = FallbackEvent(
                from_provider=self._primary.name,
                from_model=model_id,
                to_provider=self._fallback.name,
                to_model=self._fallback.default_model,
                reason=str(primary_exc)[:300],
            )
            log.warning(
                "llm.fallback",
                from_provider=event.from_provider,
                from_model=event.from_model,
                to_provider=event.to_provider,
                to_model=event.to_model,
                reason=event.reason,
            )
            try:
                # The per-call `model` override is a PRIMARY model id; the
                # fallback always uses its own configured model.
                result = await self._fallback.call_model(
                    prompt, model=self._fallback.default_model, **kwargs
                )
            except LLMError as fallback_exc:
                raise LLMError(
                    "both providers failed — "
                    f"{self._primary.name} ({model_id}): {primary_exc}; "
                    f"{self._fallback.name} ({self._fallback.default_model}): {fallback_exc}",
                    model=model_id,
                    provider=self._primary.name,
                ) from fallback_exc
            result = dataclasses.replace(result, fallback=True)
            log.info(
                "llm.call_finished",
                provider=result.provider,
                model=result.model,
                prompt_tokens=result.tokens_in,
                completion_tokens=result.tokens_out,
                latency_ms=result.latency_ms,
                fallback=True,
            )
            return result, event
        log.info(
            "llm.call_finished",
            provider=result.provider,
            model=result.model,
            prompt_tokens=result.tokens_in,
            completion_tokens=result.tokens_out,
            latency_ms=result.latency_ms,
            fallback=False,
        )
        return result, None

    # ------------------------------------------------------------------ text

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
        """One completion (with failover). Returns text + token/cost accounting."""
        result, _event = await self._call_with_failover(
            prompt,
            model=model,
            system=system,
            json_schema=json_schema,
            temperature=temperature,
            max_tokens=max_tokens,
            disable_thinking=disable_thinking,
        )
        return result

    def call_model_sync(self, prompt: str, **kwargs: Any) -> LLMResult:
        """Blocking wrapper for synchronous call sites (e.g. sync graph nodes)."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.call_model(prompt, **kwargs))
        raise RuntimeError(
            "call_model_sync() cannot run inside an active event loop; await call_model()."
        )

    # -------------------------------------------------------------- batching

    async def classify_batch(
        self,
        views: Sequence[ClassifierView],
        *,
        instructions: str,
        item_schema: Mapping[str, Any],
        model: str | None = None,
        system: str | None = None,
        max_attempts: int = 2,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> BatchClassification:
        """Classify up to ~25 threads in **one** LLM call, schema-validated.

        ``views`` must be :class:`ClassifierView` instances — the only shape
        that can reach a prompt; anything else raises ``TypeError`` (privacy by
        type: an email body is unrepresentable). Results are keyed by
        ``thread_id``. Anything the model omitted or malformed is reported via
        ``missing_ids`` / ``invalid`` so the caller can degrade it to
        needs-review — never silently guess.
        """
        for index, view in enumerate(views):
            if not isinstance(view, ClassifierView):
                raise TypeError(
                    f"classify_batch accepts only ClassifierView instances; item {index} "
                    f"is {type(view).__name__}. Build a ClassifierView — email bodies "
                    "are unrepresentable in the classifier input by design."
                )
        if not views:
            return BatchClassification(id_field=_ID_FIELD)

        expected_ids = [view.thread_id for view in views]
        if len(set(expected_ids)) != len(expected_ids):
            raise ValueError(f"duplicate {_ID_FIELD} values in batch")

        validator = Draft202012Validator(dict(item_schema))
        model_id = model or self._default_model
        items = [view.to_prompt_dict() for view in views]
        prompt = _build_batch_prompt(items, instructions, item_schema, expected_ids)
        views_by_id = {view.thread_id: view for view in views}

        results: dict[str, dict[str, Any]] = {}
        invalid: list[dict[str, Any]] = []
        calls: list[LLMResult] = []
        fallback_events: list[FallbackEvent] = []
        attempts = 0
        last_error: Exception | None = None
        budget = max_tokens

        for attempt in range(1, max_attempts + 1):
            missing = [i for i in expected_ids if i not in results]
            if not missing:
                break
            attempts = attempt
            attempt_prompt = prompt if attempt == 1 else _retry_prompt(prompt, missing)
            if attempt > 1:
                _log().info(
                    "llm.batch_retry",
                    model=model_id,
                    attempt=attempt,
                    missing=len(missing),
                    error=type(last_error).__name__ if last_error else None,
                )
            try:
                result, event = await self._call_with_failover(
                    attempt_prompt,
                    model=model_id,
                    system=system or _BATCH_SYSTEM,
                    json_schema=_batch_schema(item_schema),
                    disable_thinking=True,
                    temperature=temperature,
                    max_tokens=budget,
                )
            except LLMError as exc:
                # Both providers failed for this attempt: surface with the spend
                # accumulated so far so the caller can persist it.
                raise LLMError(
                    str(exc),
                    usage=_aggregate_usage(calls, model_id, attempts),
                    model=exc.model,
                    provider=exc.provider,
                ) from exc
            calls.append(result)
            if event is not None:
                fallback_events.append(event)

            if result.finish_reason == "length":
                # The reply hit the token ceiling and is truncated (often empty
                # for reasoning models). Re-sending the same oversized request
                # would burn the same budget again — split the work instead.
                if len(missing) > 1:
                    half = len(missing) // 2
                    for chunk_ids in (missing[:half], missing[half:]):
                        chunk = [views_by_id[i] for i in chunk_ids]
                        try:
                            sub = await self.classify_batch(
                                chunk,
                                instructions=instructions,
                                item_schema=item_schema,
                                model=model,
                                system=system,
                                max_attempts=max_attempts,
                                max_tokens=max_tokens,
                                temperature=temperature,
                            )
                        except LLMSchemaError as exc:
                            last_error = exc
                            continue
                        for entry in sub.results:
                            results[str(entry[_ID_FIELD])] = entry
                        invalid.extend(sub.invalid)
                        calls.extend(sub.calls)
                        fallback_events.extend(sub.fallback_events)
                        if sub.usage is not None:
                            attempts += sub.usage.attempts
                    break
                # A single item that still truncates needs budget, not repetition.
                budget *= 2
                last_error = LLMSchemaError(
                    "response truncated at the token limit (finish_reason=length)"
                )
                continue

            try:
                parsed = _extract_results(result.text)
            except LLMSchemaError as exc:
                last_error = exc
                continue
            for entry in parsed:
                if not isinstance(entry, dict):
                    invalid.append({"raw": entry, "error": "not an object"})
                    continue
                entry_id = str(entry.get(_ID_FIELD, ""))
                if entry_id not in set(expected_ids) or entry_id in results:
                    invalid.append({"raw": entry, "error": f"unknown or duplicate {_ID_FIELD}"})
                    continue
                errors = sorted(validator.iter_errors(entry), key=str)
                if errors:
                    invalid.append({"raw": entry, "error": errors[0].message})
                    continue
                results[entry_id] = entry

        missing_ids = [i for i in expected_ids if i not in results]
        usage = _aggregate_usage(calls, model_id, attempts)
        if not results and last_error is not None:
            # The accumulated usage rides on the error so the caller can persist
            # the spend of a failed batch.
            raise LLMSchemaError(
                f"no parsable results after {attempts} attempt(s): {last_error}",
                usage=usage,
            ) from last_error

        return BatchClassification(
            results=[results[i] for i in expected_ids if i in results],
            missing_ids=missing_ids,
            invalid=invalid,
            usage=usage,
            calls=calls,
            fallback_events=fallback_events,
            id_field=_ID_FIELD,
        )


# ---------------------------------------------------------------- helpers


def _aggregate_usage(calls: Sequence[LLMResult], model_id: str, attempts: int) -> LLMResult:
    tokens_in = sum(c.tokens_in for c in calls)
    tokens_out = sum(c.tokens_out for c in calls)
    return LLMResult(
        text="",
        model=calls[-1].model if calls else model_id,
        provider=calls[-1].provider if calls else "",
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=sum(c.latency_ms for c in calls),
        usd=sum(c.usd for c in calls),
        attempts=max(attempts, len(calls)),
        fallback=any(c.fallback for c in calls),
    )


def _batch_schema(item_schema: Mapping[str, Any]) -> dict[str, Any]:
    """The wire-level ``response_format`` schema: ``{"results": [<item>, ...]}``."""
    return {
        "type": "object",
        "required": ["results"],
        "properties": {"results": {"type": "array", "items": dict(item_schema)}},
        "additionalProperties": False,
    }


def _build_batch_prompt(
    items: Sequence[Mapping[str, Any]],
    instructions: str,
    item_schema: Mapping[str, Any],
    expected_ids: Sequence[str],
) -> str:
    return (
        f"{instructions.strip()}\n\n"
        f"Classify EVERY one of the {len(expected_ids)} items below — exactly one "
        f"result object per item, with the same {_ID_FIELD}.\n\n"
        "Each result object MUST validate against this JSON Schema:\n"
        f"{json.dumps(dict(item_schema), indent=2)}\n\n"
        "ITEMS:\n"
        f"{json.dumps(list(items), indent=2, default=str)}\n\n"
        'Reply with a single JSON object of the form {"results": [ ... ]} containing '
        f"exactly {len(expected_ids)} result objects, in the same order as the items. "
        "Output JSON only."
    )


def _retry_prompt(prompt: str, missing: Sequence[str]) -> str:
    return (
        f"{prompt}\n\nYour previous reply was incomplete or invalid. Return results "
        f"for these {_ID_FIELD} values only: {json.dumps(list(missing))}. "
        'Reply with {"results": [ ... ]} and nothing else.'
    )


def _extract_results(text: str) -> list[Any]:
    """Pull the ``results`` array out of a model reply, tolerating fences/preamble."""
    payload = _loads_forgiving(text)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("results", "items", "classifications", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        raise LLMSchemaError(f"JSON object has no results array (keys: {sorted(payload)})")
    raise LLMSchemaError(f"unexpected JSON payload type: {type(payload).__name__}")


def _loads_forgiving(text: str) -> Any:
    candidates: list[str] = []
    stripped = (text or "").strip()
    if not stripped:
        raise LLMSchemaError("model returned an empty response")
    candidates.append(stripped)
    fenced = _JSON_FENCE.search(stripped)
    if fenced:
        candidates.append(fenced.group(1))
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = stripped.find(opener), stripped.rfind(closer)
        if start != -1 and end > start:
            candidates.append(stripped[start : end + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise LLMSchemaError(f"response was not valid JSON: {stripped[:200]!r}")


_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    """Process-wide singleton client, built from settings on first use."""
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


def reset_llm_client() -> None:
    """Drop the cached client (used by tests and after a settings change)."""
    global _client
    _client = None
