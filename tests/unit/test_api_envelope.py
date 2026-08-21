"""Slice db-and-domain — the {ok, data|error} envelope (src/api/_common.py)."""

from datetime import datetime, timezone

from api._common import (
    GMAIL_RECONNECT,
    GMAIL_RECONNECT_MESSAGE,
    SIGNED_OUT,
    api_error,
    error_body,
    gmail_reconnect,
    iso,
    not_found,
    ok,
    signed_out,
)


class TestEnvelope:
    def test_ok_shape(self):
        assert ok({"a": 1}) == {"ok": True, "data": {"a": 1}}

    def test_error_shape(self):
        assert error_body("x", "y") == {"ok": False, "error": {"code": "x", "message": "y"}}


class TestErrorCodes:
    def test_signed_out_is_401(self):
        exc = signed_out()
        assert exc.status_code == 401
        assert exc.detail["code"] == SIGNED_OUT

    def test_gmail_reconnect_uses_the_canonical_sentence(self):
        exc = gmail_reconnect()
        assert exc.detail == {"code": GMAIL_RECONNECT, "message": GMAIL_RECONNECT_MESSAGE}
        assert exc.detail["message"] == "Reconnect Gmail to continue."

    def test_status_mapping_and_override(self):
        assert not_found("Run").status_code == 404
        assert api_error("validation_error", "bad").status_code == 422
        assert api_error("conflict", "busy").status_code == 409
        assert api_error("custom_code", "m").status_code == 400
        assert api_error("custom_code", "m", status_code=418).status_code == 418


class TestIso:
    def test_none_passthrough(self):
        assert iso(None) is None

    def test_datetime_serializes(self):
        dt = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        assert iso(dt) == "2026-01-02T03:04:05+00:00"
