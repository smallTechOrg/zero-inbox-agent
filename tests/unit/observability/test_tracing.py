"""LangSmith tracing activates only when a key is present, and never leaks it."""

from __future__ import annotations

import json

import pytest

from observability import configure_logging
from observability.tracing import (
    configure_tracing,
    trace_config,
    tracing_enabled,
    tracing_key_present,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("LANGCHAIN_API_KEY", "LANGCHAIN_TRACING_V2", "LANGCHAIN_PROJECT"):
        monkeypatch.delenv(var, raising=False)
    configure_logging("INFO", force=True)


# --- happy path -----------------------------------------------------------------


def test_tracing_is_enabled_when_api_key_is_present(monkeypatch):
    monkeypatch.setenv("LANGCHAIN_API_KEY", "lsv2_pt_fake_key_value")

    enabled = configure_tracing("zero-inbox-test")

    assert enabled is True
    assert tracing_enabled() is True
    import os

    assert os.environ["LANGCHAIN_TRACING_V2"] == "true"
    assert os.environ["LANGCHAIN_PROJECT"] == "zero-inbox-test"


# --- edge cases -----------------------------------------------------------------


def test_tracing_is_disabled_when_no_api_key():
    enabled = configure_tracing()

    assert enabled is False
    assert tracing_enabled() is False
    assert tracing_key_present() is False


def test_blank_api_key_counts_as_absent(monkeypatch):
    monkeypatch.setenv("LANGCHAIN_API_KEY", "   ")

    assert configure_tracing() is False


def test_trace_config_works_without_tracing_enabled():
    config = trace_config("triage_run", run_id="r1")

    assert config["run_name"] == "triage_run"
    assert "zero-inbox" in config["tags"]
    assert "langsmith" not in config["tags"]
    assert config["metadata"]["run_id"] == "r1"


def test_trace_config_tags_langsmith_when_enabled(monkeypatch):
    monkeypatch.setenv("LANGCHAIN_API_KEY", "lsv2_pt_fake_key_value")
    configure_tracing("zero-inbox-test")

    assert "langsmith" in trace_config("triage_run")["tags"]


# --- error / privacy path -------------------------------------------------------


def test_enabling_tracing_never_logs_the_api_key(monkeypatch, capsys):
    monkeypatch.setenv("LANGCHAIN_API_KEY", "lsv2_pt_supersecretvalue")

    configure_tracing("zero-inbox-test")

    out = capsys.readouterr().out
    assert "lsv2_pt_supersecretvalue" not in out
    record = json.loads([l for l in out.splitlines() if l.strip()][-1])
    assert record["event"] == "tracing.enabled"
    assert "api_key" not in record


def test_trace_config_carries_no_secret_material(monkeypatch):
    monkeypatch.setenv("LANGCHAIN_API_KEY", "lsv2_pt_supersecretvalue")
    configure_tracing()

    assert "lsv2_pt_supersecretvalue" not in json.dumps(trace_config("triage_run"))
