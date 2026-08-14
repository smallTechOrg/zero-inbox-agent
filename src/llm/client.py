"""The LLM layer's public surface.

Everything in the app talks to :class:`LLMClient`; nothing imports a provider
directly. The model id is **always** swappable per call (Phase 3 adds a UI
dropdown that simply passes ``model=`` through — no code change needed).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Iterable, Mapping, Sequence

from jsonschema import Draft202012Validator

from config.settings import get_settings
from llm.health import ProviderCircuitOpen
from llm.providers.base import (
    BatchClassification,
    LLMError,
    LLMProvider,
    LLMResult,
    LLMSchemaError,
    estimate_cost_usd,
)
from llm.providers.nvidia import NvidiaProvider

__all__ = [
    "LLMClient",
    "get_llm_client",
    "reset_llm_client",
    "BatchClassification",
    "LLMResult",
    "LLMError",
    "LLMSchemaError",
    # Re-exported so call sites can catch it without importing llm.health directly.
    # It is NOT an LLMError: it must never be swallowed by a retry/degrade path.
    "ProviderCircuitOpen",
]

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _log() -> Any:
    """Structured logger. Every line here is bridged to the user's SSE feed by
    ``observability.logging.activity_bus_processor`` — a single LLM call over a
    whole batch must not be a silent minute in the UI. ``run_id``/``user_id``
    come from the structlog contextvars the caller bound; without a ``user_id``
    the bridge drops the event, which is the intended default off-run."""
    from observability.logging import get_logger

    return get_logger("zero_inbox.llm")

_BATCH_SYSTEM = (
    "You are a precise classification engine. You classify a batch of items in a "
    "single pass and reply with JSON only — no prose, no markdown fences, no "
    "explanation outside the JSON."
)


def make_provider(settings: Any | None = None) -> LLMProvider:
    """Build the configured provider. NVIDIA NIM is the only provider in v1."""
    s = settings or get_settings()
    return NvidiaProvider(
        api_key=s.nvidia_api_key,
        base_url=s.nvidia_base_url,
        default_model=s.nvidia_default_model,
        timeout_s=s.llm_timeout_seconds,
        max_retries=s.llm_max_retries,
    )


class LLMClient:
    """Thin, async, provider-agnostic entry point to the LLM."""

    def __init__(
        self,
        provider: LLMProvider | None = None,
        *,
        default_model: str | None = None,
    ) -> None:
        self._provider = provider if provider is not None else make_provider()
        self._default_model = default_model or self._provider.default_model

    @property
    def default_model(self) -> str:
        return self._default_model

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
        """One completion. Returns text plus token/cost/latency accounting."""
        model_id = model or self._default_model
        log = _log()
        started = time.perf_counter()
        log.info("llm.call_started", model=model_id, prompt_chars=len(prompt or ""))
        try:
            result = await self._provider.call_model(
                prompt,
                system=system,
                model=model_id,
                json_schema=json_schema,
                temperature=temperature,
                max_tokens=max_tokens,
                disable_thinking=disable_thinking,
            )
        except Exception as exc:
            log.warning(
                "llm.call_failed",
                model=model_id,
                latency_ms=int((time.perf_counter() - started) * 1000),
                error=type(exc).__name__,
            )
            raise
        log.info(
            "llm.call_finished",
            model=result.model,
            prompt_tokens=result.tokens_in,
            completion_tokens=result.tokens_out,
            latency_ms=result.latency_ms
            or int((time.perf_counter() - started) * 1000),
        )
        return result

    def call_model_sync(self, prompt: str, **kwargs: Any) -> LLMResult:
        """Blocking convenience wrapper for synchronous callers (e.g. sync graph nodes).

        Raises if called from inside a running event loop — use ``call_model`` there.
        """
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
        items: Sequence[Mapping[str, Any]],
        *,
        instructions: str,
        item_schema: Mapping[str, Any],
        id_field: str = "item_id",
        model: str | None = None,
        system: str | None = None,
        max_attempts: int = 2,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> BatchClassification:
        """Classify 20–50 items in **one** call, validating each result against a schema.

        ``items`` are dicts that must each carry ``id_field``. Returns only
        schema-conformant results; anything the model omitted or malformed is
        reported via ``missing_ids`` / ``invalid`` so the caller can degrade it
        safely (never silently guess).

        Reasoning-model discipline (Nemotron): every call sends the JSON schema as
        a real ``response_format`` and disables thinking; a ``finish_reason ==
        "length"`` reply is truncated, so it is **never** parse-and-retried — a
        multi-item batch is split in half and re-issued, and a single item gets a
        doubled token budget instead.
        """
        if not items:
            return BatchClassification(id_field=id_field)

        expected_ids = [str(_require_id(item, id_field, index)) for index, item in enumerate(items)]
        if len(set(expected_ids)) != len(expected_ids):
            raise ValueError(f"duplicate {id_field} values in batch")

        validator = Draft202012Validator(dict(item_schema))
        model_id = model or self._default_model
        prompt = _build_batch_prompt(items, instructions, item_schema, id_field, expected_ids)
        items_by_id = {str(item[id_field]): item for item in items}

        results: dict[str, dict[str, Any]] = {}
        invalid: list[dict[str, Any]] = []
        tokens_in = tokens_out = latency_ms = 0
        attempts = 0
        last_error: Exception | None = None
        actual_model = model_id
        budget = max_tokens

        for attempt in range(1, max_attempts + 1):
            missing = [i for i in expected_ids if i not in results]
            if not missing:
                break
            attempts = attempt
            attempt_prompt = prompt if attempt == 1 else _retry_prompt(prompt, missing, id_field)
            log = _log()
            if attempt > 1:
                log.info(
                    "llm.retry",
                    model=model_id,
                    attempt=attempt,
                    missing=len(missing),
                    error=type(last_error).__name__ if last_error else None,
                    backoff_ms=0,
                )
            started = time.perf_counter()
            log.info(
                "llm.call_started",
                model=model_id,
                batch_size=len(missing),
                attempt=attempt,
            )
            try:
                result = await self._provider.call_model(
                    attempt_prompt,
                    system=system or _BATCH_SYSTEM,
                    model=model_id,
                    json_schema=_batch_schema(item_schema),
                    disable_thinking=True,
                    temperature=temperature,
                    max_tokens=budget,
                )
            except Exception as exc:
                log.warning(
                    "llm.call_failed",
                    model=model_id,
                    attempt=attempt,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    error=type(exc).__name__,
                )
                raise
            log.info(
                "llm.call_finished",
                model=result.model,
                prompt_tokens=result.tokens_in,
                completion_tokens=result.tokens_out,
                attempt=attempt,
                latency_ms=result.latency_ms
                or int((time.perf_counter() - started) * 1000),
            )
            tokens_in += result.tokens_in
            tokens_out += result.tokens_out
            latency_ms += result.latency_ms
            actual_model = result.model

            if getattr(result, "finish_reason", None) == "length":
                # The reply hit the token ceiling and is truncated (often empty for
                # reasoning models). Re-sending the same oversized request would burn
                # the same budget again — split the work instead.
                if len(missing) > 1:
                    half = len(missing) // 2
                    for chunk_ids in (missing[:half], missing[half:]):
                        chunk = [items_by_id[i] for i in chunk_ids]
                        try:
                            sub = await self.classify_batch(
                                chunk,
                                instructions=instructions,
                                item_schema=item_schema,
                                id_field=id_field,
                                model=model,
                                system=system,
                                max_attempts=max_attempts,
                                max_tokens=max_tokens,
                                temperature=temperature,
                            )
                        except LLMSchemaError as exc:
                            last_error = exc
                            sub_usage = exc.usage
                            if sub_usage is not None:
                                tokens_in += sub_usage.tokens_in
                                tokens_out += sub_usage.tokens_out
                                latency_ms += sub_usage.latency_ms
                                attempts += sub_usage.attempts
                            continue
                        for entry in sub.results:
                            results[str(entry[id_field])] = entry
                        invalid.extend(sub.invalid)
                        if sub.usage is not None:
                            tokens_in += sub.usage.tokens_in
                            tokens_out += sub.usage.tokens_out
                            latency_ms += sub.usage.latency_ms
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
                entry_id = str(entry.get(id_field, ""))
                if entry_id not in set(expected_ids) or entry_id in results:
                    invalid.append({"raw": entry, "error": f"unknown or duplicate {id_field}"})
                    continue
                errors = sorted(validator.iter_errors(entry), key=str)
                if errors:
                    invalid.append({"raw": entry, "error": errors[0].message})
                    continue
                results[entry_id] = entry

        missing_ids = [i for i in expected_ids if i not in results]
        usage = LLMResult(
            text="",
            model=actual_model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            usd=estimate_cost_usd(model_id, tokens_in, tokens_out),
            attempts=attempts,
        )
        if not results and last_error is not None:
            # The accumulated usage rides on the error so the caller can persist
            # the spend of a failed batch (purpose="classify_failed").
            raise LLMSchemaError(
                f"no parsable results after {attempts} attempt(s): {last_error}",
                usage=usage,
            ) from last_error

        return BatchClassification(
            results=[results[i] for i in expected_ids if i in results],
            missing_ids=missing_ids,
            invalid=invalid,
            usage=usage,
            id_field=id_field,
        )


# ---------------------------------------------------------------- helpers


def _batch_schema(item_schema: Mapping[str, Any]) -> dict[str, Any]:
    """The wire-level ``response_format`` schema: ``{"results": [<item>, ...]}``."""
    return {
        "type": "object",
        "required": ["results"],
        "properties": {"results": {"type": "array", "items": dict(item_schema)}},
        "additionalProperties": False,
    }


def _require_id(item: Mapping[str, Any], id_field: str, index: int) -> Any:
    if id_field not in item or item[id_field] in (None, ""):
        raise ValueError(f"item at index {index} is missing required {id_field!r}")
    return item[id_field]


def _build_batch_prompt(
    items: Iterable[Mapping[str, Any]],
    instructions: str,
    item_schema: Mapping[str, Any],
    id_field: str,
    expected_ids: Sequence[str],
) -> str:
    return (
        f"{instructions.strip()}\n\n"
        f"Classify EVERY one of the {len(expected_ids)} items below — exactly one "
        f"result object per item, with the same {id_field}.\n\n"
        "Each result object MUST validate against this JSON Schema:\n"
        f"{json.dumps(dict(item_schema), indent=2)}\n\n"
        "ITEMS:\n"
        f"{json.dumps(list(items), indent=2, default=str)}\n\n"
        'Reply with a single JSON object of the form {"results": [ ... ]} containing '
        f"exactly {len(expected_ids)} result objects, in the same order as the items. "
        "Output JSON only."
    )


def _retry_prompt(prompt: str, missing: Sequence[str], id_field: str) -> str:
    return (
        f"{prompt}\n\nYour previous reply was incomplete or invalid. Return results "
        f"for these {id_field} values only: {json.dumps(list(missing))}. "
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
