"""LangSmith tracing wiring.

Tracing is *optional*: it activates only when ``LANGCHAIN_API_KEY`` is present in the
environment. When the key is absent, tracing is explicitly disabled and the app runs
normally — structured stdout logging (always on) remains the observability floor.

The API key itself is never read into a log line or returned by any function here;
only its presence is reported.
"""

from __future__ import annotations

import os
from typing import Any

from observability.logging import get_logger

DEFAULT_PROJECT = "zero-inbox-agent"


def tracing_key_present() -> bool:
    """True when a LangSmith API key is configured (presence only, never the value)."""
    return bool((os.getenv("LANGCHAIN_API_KEY") or "").strip())


def tracing_enabled() -> bool:
    """True when LangSmith tracing is currently switched on."""
    return (
        os.getenv("LANGCHAIN_TRACING_V2", "").strip().lower() == "true"
        and tracing_key_present()
    )


def configure_tracing(project: str | None = None) -> bool:
    """Enable LangSmith tracing iff an API key is present. Returns whether it is on."""
    log = get_logger("zero_inbox.tracing")
    if not tracing_key_present():
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
        log.info("tracing.disabled", reason="LANGCHAIN_API_KEY not set")
        return False

    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ.setdefault(
        "LANGCHAIN_ENDPOINT", "https://api.smith.langchain.com"
    )
    resolved_project = project or os.getenv("LANGCHAIN_PROJECT") or DEFAULT_PROJECT
    os.environ["LANGCHAIN_PROJECT"] = resolved_project
    log.info("tracing.enabled", provider="langsmith", project=resolved_project)
    return True


def trace_config(run_name: str, **metadata: Any) -> dict[str, Any]:
    """Build a LangGraph/LangChain ``RunnableConfig`` fragment for a traced run.

    Always safe to pass through: it carries no secrets, only run naming/metadata,
    and works identically whether or not tracing is enabled.
    """
    tags = ["zero-inbox", "phase-1"]
    if tracing_enabled():
        tags.append("langsmith")
    return {
        "run_name": run_name,
        "tags": tags,
        "metadata": {"project": os.getenv("LANGCHAIN_PROJECT", DEFAULT_PROJECT), **metadata},
    }
