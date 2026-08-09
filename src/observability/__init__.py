"""Observability: unconditional structured stdout logging + optional LangSmith tracing."""

from observability.logging import (
    BODY_FIELDS,
    REDACTED,
    SECRET_FIELDS,
    configure_logging,
    get_logger,
    log_operation,
    redact_processor,
)
from observability.tracing import (
    configure_tracing,
    trace_config,
    tracing_enabled,
    tracing_key_present,
)

__all__ = [
    "BODY_FIELDS",
    "REDACTED",
    "SECRET_FIELDS",
    "configure_logging",
    "configure_tracing",
    "get_logger",
    "log_operation",
    "redact_processor",
    "setup_observability",
    "trace_config",
    "tracing_enabled",
    "tracing_key_present",
]


def setup_observability(log_level: str | None = None, project: str | None = None) -> bool:
    """One-call startup hook: logging always, tracing when a LangSmith key exists."""
    configure_logging(log_level)
    return configure_tracing(project)
