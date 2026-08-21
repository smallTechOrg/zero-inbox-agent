"""Named application events, logged with a consistent shape.

Every helper here goes through :mod:`src.observability.logging`, so secrets and email
bodies are scrubbed before anything reaches stdout.
"""

from __future__ import annotations

from typing import Any

from observability.logging import configure_logging, get_logger, log_operation

__all__ = [
    "configure_logging",
    "get_logger",
    "log_operation",
    "log_http_request",
    "log_llm_call",
    "log_triage_progress",
]


def log_http_request(
    method: str,
    path: str,
    status: int,
    latency_ms: int,
    *,
    user_id: str | int | None = None,
    error: str | None = None,
    **fields: Any,
) -> None:
    log = get_logger("zero_inbox.http")
    event = {
        "method": method,
        "path": path,
        "status": status,
        "latency_ms": latency_ms,
        "user_id": user_id,
        **fields,
    }
    if error:
        log.error("http.request", error=error, **event)
    else:
        log.info("http.request", **event)


def log_llm_call(
    model: str,
    tokens_in: int,
    tokens_out: int,
    latency_ms: int,
    *,
    batch_size: int | None = None,
    error: str | None = None,
    **fields: Any,
) -> None:
    """Log an LLM call. Prompt and completion text are deliberately NOT logged."""
    log = get_logger("zero_inbox.llm")
    event = {
        "model": model,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "latency_ms": latency_ms,
        "batch_size": batch_size,
        **fields,
    }
    if error:
        log.error("llm.call", error=error, **event)
    else:
        log.info("llm.call", **event)


def log_triage_progress(
    run_id: str | int,
    items_total: int,
    items_decided: int,
    *,
    tier: str | None = None,
    **fields: Any,
) -> None:
    get_logger("zero_inbox.triage").info(
        "triage.progress",
        run_id=run_id,
        items_total=items_total,
        items_decided=items_decided,
        tier=tier,
        **fields,
    )
