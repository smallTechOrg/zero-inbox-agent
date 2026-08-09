"""Structured logging: happy path, edge cases, and error path."""

from __future__ import annotations

import json

import pytest

from observability import configure_logging, get_logger, log_operation
from observability.events import log_llm_call, log_triage_progress
from observability.logging import REDACTED, redact_processor


@pytest.fixture(autouse=True)
def _configured():
    configure_logging("INFO", force=True)


def _last_json(capsys) -> dict:
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines, "expected at least one log line on stdout"
    return json.loads(lines[-1])


# --- happy path -----------------------------------------------------------------


def test_llm_call_logs_json_with_model_tokens_and_latency(capsys):
    log_llm_call("nvidia/nemotron-3-nano-30b-a3b", tokens_in=120, tokens_out=44, latency_ms=812, batch_size=20)

    record = _last_json(capsys)

    assert record["event"] == "llm.call"
    assert record["model"] == "nvidia/nemotron-3-nano-30b-a3b"
    assert record["tokens_in"] == 120
    assert record["latency_ms"] == 812
    assert record["level"] == "info"
    assert "timestamp" in record


def test_log_operation_emits_start_and_finish_with_latency(capsys):
    with log_operation("triage.run", run_id="r1") as extra:
        extra["items_total"] = 220

    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]
    events = [line["event"] for line in lines]

    assert events == ["operation.start", "operation.finish"]
    assert lines[-1]["items_total"] == 220
    assert isinstance(lines[-1]["latency_ms"], int)


# --- privacy / edge cases -------------------------------------------------------


def test_secret_fields_are_never_rendered(capsys):
    get_logger("test").info("connect", api_key="nvapi-should-not-appear", refresh_token="tok-123")

    record = _last_json(capsys)

    assert record["api_key"] == REDACTED
    assert record["refresh_token"] == REDACTED
    assert "nvapi-should-not-appear" not in json.dumps(record)


def test_email_body_and_snippet_are_never_rendered(capsys):
    get_logger("test").info(
        "item.seen",
        subject="Invoice due",
        snippet="Hi Sai, your card ending 4242 ...",
        body="full private body text",
    )

    record = _last_json(capsys)

    assert record["subject"] == "Invoice due"
    assert record["snippet"] == "[OMITTED:body]"
    assert record["body"] == "[OMITTED:body]"


def test_secret_shaped_values_inside_free_text_are_scrubbed(capsys):
    get_logger("test").info(
        "provider.error", detail="401 from key nvapi-abcdefghijklmnop rejected"
    )

    record = _last_json(capsys)

    assert "nvapi-abcdefghijklmnop" not in record["detail"]
    assert REDACTED in record["detail"]


def test_nested_secret_in_dict_field_is_scrubbed():
    scrubbed = redact_processor(
        None, "info", {"event": "x", "ctx": {"client_secret": "GOCSPX-abcdefghij", "id": 7}}
    )

    assert scrubbed["ctx"]["client_secret"] == REDACTED
    assert scrubbed["ctx"]["id"] == 7


def test_zero_and_empty_values_still_log_without_error(capsys):
    log_triage_progress(run_id="", items_total=0, items_decided=0)

    record = _last_json(capsys)

    assert record["items_total"] == 0
    assert record["items_decided"] == 0


# --- error path -----------------------------------------------------------------


def test_log_operation_logs_error_and_reraises(capsys):
    with pytest.raises(ValueError):
        with log_operation("triage.run", run_id="r2"):
            raise ValueError("boom")

    record = _last_json(capsys)

    assert record["event"] == "operation.error"
    assert record["level"] == "error"
    assert record["error"] == "ValueError"
    assert record["error_message"] == "boom"
    assert isinstance(record["latency_ms"], int)


def test_failed_llm_call_logs_at_error_level(capsys):
    log_llm_call("nvidia/nemotron-3-nano-30b-a3b", 0, 0, 30, error="rate_limited")

    record = _last_json(capsys)

    assert record["level"] == "error"
    assert record["error"] == "rate_limited"
