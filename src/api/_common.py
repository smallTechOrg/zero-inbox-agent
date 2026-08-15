"""Shared HTTP helpers — the JSON envelope, error codes, small serializers.

Every route returns ``{"ok": true, "data": …}`` via :func:`ok`, or raises
:func:`api_error`, rendered by the handlers in :mod:`api.app` as
``{"ok": false, "error": {"code": …, "message": …}}`` (spec/api.md). Error
messages are human-actionable sentences — never tracebacks.
"""

from datetime import datetime
from typing import Any

from fastapi import HTTPException

# Canonical error codes (spec/api.md).
SIGNED_OUT = "signed_out"                # 401 — no/invalid session cookie
GMAIL_RECONNECT = "gmail_reconnect"      # any Google token failure, anywhere
NOT_FOUND = "not_found"
CONFLICT = "conflict"                    # 409 — active run, needs-review delete, …
VALIDATION_ERROR = "validation_error"
RATE_LIMITED = "rate_limited"
PROVIDER_ERROR = "provider_error"

#: The one sentence every Google-token failure surfaces as (spec/architecture.md).
GMAIL_RECONNECT_MESSAGE = "Reconnect Gmail to continue."

_STATUS_FOR_CODE = {
    SIGNED_OUT: 401,
    GMAIL_RECONNECT: 409,
    NOT_FOUND: 404,
    CONFLICT: 409,
    VALIDATION_ERROR: 422,
    RATE_LIMITED: 429,
    PROVIDER_ERROR: 502,
}


def ok(data: Any) -> dict:
    return {"ok": True, "data": data}


def error_body(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


def api_error(code: str, message: str, status_code: int | None = None) -> HTTPException:
    return HTTPException(
        status_code=status_code or _STATUS_FOR_CODE.get(code, 400),
        detail={"code": code, "message": message},
    )


def signed_out() -> HTTPException:
    return api_error(SIGNED_OUT, "You are signed out — sign in with Google to continue.")


def gmail_reconnect() -> HTTPException:
    return api_error(GMAIL_RECONNECT, GMAIL_RECONNECT_MESSAGE)


def not_found(what: str) -> HTTPException:
    return api_error(NOT_FOUND, f"{what} not found")


def iso(value: datetime | None) -> str | None:
    """Timestamps cross the wire as ISO-8601 strings, or null."""
    if value is None:
        return None
    return value.isoformat()
