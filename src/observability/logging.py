"""Structured stdout logging for Zero Inbox Agent.

Unconditional: JSON logs always go to stdout, whether or not tracing is configured.

Two hard privacy rules are enforced here rather than at every call site:

* secret-shaped fields (api keys, tokens, cookies, passwords) are never rendered;
* email body/snippet text is never rendered.

Both are replaced with a marker so the *shape* of an event stays debuggable while
its sensitive payload never reaches stdout.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from contextlib import contextmanager
from typing import Any, Iterator, MutableMapping

import structlog

REDACTED = "[REDACTED]"

#: Field names whose values are secrets and must never be logged.
SECRET_FIELDS: frozenset[str] = frozenset(
    {
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "token",
        "id_token",
        "authorization",
        "auth",
        "client_secret",
        "secret",
        "secret_key",
        "password",
        "passwd",
        "cookie",
        "session_cookie",
        "zi_session",
        "state",
        "code",
        "nvidia_api_key",
        "langchain_api_key",
        "google_client_secret",
    }
)

#: Field names carrying email content, which must never leave the process in a log.
BODY_FIELDS: frozenset[str] = frozenset(
    {
        "body",
        "body_text",
        "body_html",
        "snippet",
        "snippet_redacted",
        "content",
        "message_body",
        "raw",
        "payload",
    }
)

#: Value-level guard for secrets that arrive inside free text.
_SECRET_VALUE_PATTERNS = (
    re.compile(r"nvapi-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"ghp_[A-Za-z0-9]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{12,}"),
    re.compile(r"ya29\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"lsv2_[A-Za-z0-9_\-]{8,}"),
    re.compile(r"GOCSPX-[A-Za-z0-9_\-]{8,}"),
)


def _scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        for pattern in _SECRET_VALUE_PATTERNS:
            value = pattern.sub(REDACTED, value)
        return value
    if isinstance(value, dict):
        return {k: _scrub_pair(k, v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_value(v) for v in value]
    return value


def _scrub_pair(key: str, value: Any) -> Any:
    lowered = str(key).lower()
    if lowered in SECRET_FIELDS:
        return REDACTED
    if lowered in BODY_FIELDS:
        return "[OMITTED:body]"
    return _scrub_value(value)


def redact_processor(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: drop secrets and email content from every event."""
    return {key: _scrub_pair(key, value) for key, value in event_dict.items()}


_configured = False


def configure_logging(log_level: str | None = None, *, force: bool = False) -> None:
    """Configure JSON structured logging to stdout. Idempotent unless ``force``."""
    global _configured
    if _configured and not force:
        return

    level_name = (log_level or os.getenv("AGENT_LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level, force=True)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(sort_keys=True),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=False,
    )
    _configured = True


def get_logger(name: str = "zero_inbox") -> Any:
    """Return a bound structured logger, configuring logging on first use."""
    configure_logging()
    return structlog.get_logger().bind(logger=name)


@contextmanager
def log_operation(name: str, **fields: Any) -> Iterator[dict[str, Any]]:
    """Log start/finish of an operation with latency in ms; re-raises on error.

    Extra fields collected into the yielded dict are added to the finish event.
    """
    log = get_logger("zero_inbox.op")
    extra: dict[str, Any] = {}
    started = time.perf_counter()
    log.info("operation.start", operation=name, **fields)
    try:
        yield extra
    except Exception as exc:  # noqa: BLE001 - re-raised below
        log.error(
            "operation.error",
            operation=name,
            latency_ms=int((time.perf_counter() - started) * 1000),
            error=type(exc).__name__,
            error_message=str(exc),
            **fields,
            **extra,
        )
        raise
    log.info(
        "operation.finish",
        operation=name,
        latency_ms=int((time.perf_counter() - started) * 1000),
        **fields,
        **extra,
    )
