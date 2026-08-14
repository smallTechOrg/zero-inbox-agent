"""Shared HTTP helpers: the response envelope, error codes and small serializers.

Every JSON route returns ``{"data": ..., "error": null}`` via :func:`ok`, or raises
:func:`api_error`, which FastAPI renders as ``{"data": null, "error": {code, message}}``
through the exception handler installed in :mod:`api`.
"""

from datetime import datetime
from typing import Any

from fastapi import HTTPException

# Canonical error codes (spec/api.md § Error codes).
UNAUTHENTICATED = "unauthenticated"
FORBIDDEN = "forbidden"
NOT_FOUND = "not_found"
DRY_RUN_VIOLATION = "dry_run_violation"
REAUTH_REQUIRED = "reauth_required"
RATE_LIMITED = "rate_limited"
PROVIDER_ERROR = "provider_error"
VALIDATION_ERROR = "validation_error"
#: spec/api.md:133 — the decision has not passed the never-miss reviewer and
#: can never be applied while provisional. Distinct from validation_error so
#: the UI can say WHY, and never 404 (the decision plainly exists).
NOT_REVIEWED = "not_reviewed"

_STATUS_FOR_CODE = {
    UNAUTHENTICATED: 401,
    FORBIDDEN: 403,
    NOT_FOUND: 404,
    DRY_RUN_VIOLATION: 409,
    REAUTH_REQUIRED: 409,
    RATE_LIMITED: 429,
    PROVIDER_ERROR: 502,
    VALIDATION_ERROR: 422,
}


def ok(data: Any) -> dict:
    return {"data": data, "error": None}


def api_error(code: str, message: str, status_code: int | None = None) -> HTTPException:
    return HTTPException(
        status_code=status_code or _STATUS_FOR_CODE.get(code, 400),
        detail={"code": code, "message": message},
    )


def not_found(what: str) -> HTTPException:
    return api_error(NOT_FOUND, f"{what} not found")


def iso(value: datetime | None) -> str | None:
    """Timestamps cross the wire as ISO-8601 UTC strings, or null."""
    if value is None:
        return None
    return value.isoformat()
