"""Unit tests for the Phase 6 typed emit helpers (src/events/bus.py).

These pin the documented JSON schemas in
spec/capabilities/triage-transparency.md: exact field names, exact types,
the privacy truncation caps, the absence of ANY body/unredacted content field,
and the guarantee that a raising subscriber never propagates into the run.
"""

from __future__ import annotations

import sys

import pytest


def _fresh_bus():
    """Return a freshly-imported bus module (clears module-level ring state)."""
    for name in ("events.bus", "events"):
        sys.modules.pop(name, None)
    import events.bus  # noqa: F401

    return sys.modules["events.bus"]


# Any of these appearing in an event payload is a privacy defect.
_FORBIDDEN_FIELDS = {
    "body",
    "body_text",
    "body_html",
    "snippet",
    "raw",
    "message_body",
    "content",
    "text",
}


def _assert_body_free(payload: dict) -> None:
    for key in payload:
        assert key not in _FORBIDDEN_FIELDS, f"event leaked a body field: {key}"
        assert "body" not in key, f"event leaked a body-ish field: {key}"
        assert "unredacted" not in key, f"event leaked an unredacted field: {key}"


def _only(bus, user_id: str) -> dict:
    buf = bus.replay_buffer(user_id)
    assert len(buf) == 1, f"expected exactly one event, got {len(buf)}"
    return buf[0]


# ── thread_classified ─────────────────────────────────────────────────────────


def test_thread_classified_matches_documented_schema():
    bus = _fresh_bus()
    bus.emit_thread_classified(
        "u1",
        run_id="run-1",
        item_id="item-1",
        subject="Quarterly report",
        from_email="finance@example.com",
        category="Newsletters",
        action="archive",
        decided_by="llm",
        confidence=0.92,
        reasoning="Bulk sender with no personal address",
        review_state="provisional",
    )
    ev = _only(bus, "u1")

    assert set(ev) == {
        "type",
        "run_id",
        "item_id",
        "subject",
        "from_email",
        "category",
        "action",
        "decided_by",
        "confidence",
        "reasoning",
        "review_state",
    }
    assert ev["type"] == "thread_classified"
    assert ev["run_id"] == "run-1"
    assert ev["item_id"] == "item-1"
    assert ev["subject"] == "Quarterly report"
    assert ev["from_email"] == "finance@example.com"
    assert ev["category"] == "Newsletters"
    assert ev["action"] == "archive"
    assert ev["decided_by"] == "llm"
    assert isinstance(ev["confidence"], float)
    assert ev["confidence"] == pytest.approx(0.92)
    assert ev["reasoning"] == "Bulk sender with no personal address"
    assert ev["review_state"] == "provisional"
    _assert_body_free(ev)


def test_thread_classified_truncates_subject_and_reasoning_inside_helper():
    """The cap lives in the helper so no caller can widen it."""
    bus = _fresh_bus()
    bus.emit_thread_classified(
        "u2",
        run_id="run-1",
        item_id="item-2",
        subject="S" * 500,
        from_email="a@b.com",
        category="Promotions",
        action="archive",
        decided_by="rule",
        confidence=1.0,
        reasoning="R" * 900,
        review_state="reviewed",
    )
    ev = _only(bus, "u2")
    assert len(ev["subject"]) == 60
    assert ev["subject"] == "S" * 60
    assert len(ev["reasoning"]) == 140
    assert ev["reasoning"] == "R" * 140


def test_thread_classified_coerces_none_reasoning_to_empty_string():
    bus = _fresh_bus()
    bus.emit_thread_classified(
        "u3",
        run_id="run-1",
        item_id="item-3",
        subject=None,  # type: ignore[arg-type]
        from_email=None,  # type: ignore[arg-type]
        category="Other",
        action="keep",
        decided_by="error",
        confidence=0,
        reasoning=None,  # type: ignore[arg-type]
        review_state="provisional",
    )
    ev = _only(bus, "u3")
    assert ev["subject"] == ""
    assert ev["from_email"] == ""
    assert ev["reasoning"] == ""
    assert ev["confidence"] == 0.0
    assert isinstance(ev["confidence"], float)


# ── provider_degraded ─────────────────────────────────────────────────────────


def test_provider_degraded_matches_documented_schema():
    bus = _fresh_bus()
    bus.emit_provider_degraded(
        "u4",
        run_id="run-9",
        provider="nvidia",
        model="nvidia/nemotron-3-nano-30b-a3b",
        calls=412,
        retries=2774,
        consecutive_failures=2,
    )
    ev = _only(bus, "u4")
    assert set(ev) == {
        "type",
        "run_id",
        "provider",
        "model",
        "calls",
        "retries",
        "consecutive_failures",
    }
    assert ev["type"] == "provider_degraded"
    assert ev["provider"] == "nvidia"
    assert ev["model"] == "nvidia/nemotron-3-nano-30b-a3b"
    assert isinstance(ev["calls"], int) and ev["calls"] == 412
    assert isinstance(ev["retries"], int) and ev["retries"] == 2774
    assert isinstance(ev["consecutive_failures"], int) and ev["consecutive_failures"] == 2
    _assert_body_free(ev)


# ── run_resumable ─────────────────────────────────────────────────────────────


def test_run_resumable_matches_documented_schema():
    bus = _fresh_bus()
    bus.emit_run_resumable(
        "u5",
        run_id="run-9",
        items_total=2176,
        items_decided=2003,
        reason="provider_circuit_open",
    )
    ev = _only(bus, "u5")
    assert set(ev) == {"type", "run_id", "items_total", "items_decided", "reason"}
    assert ev["type"] == "run_resumable"
    assert isinstance(ev["items_total"], int) and ev["items_total"] == 2176
    assert isinstance(ev["items_decided"], int) and ev["items_decided"] == 2003
    assert ev["reason"] == "provider_circuit_open"
    _assert_body_free(ev)


def test_run_resumable_truncates_reason():
    bus = _fresh_bus()
    bus.emit_run_resumable(
        "u6", run_id="r", items_total=1, items_decided=0, reason="x" * 400
    )
    assert len(_only(bus, "u6")["reason"]) == 140


# ── model_fallback ────────────────────────────────────────────────────────────


def test_model_fallback_matches_documented_schema():
    bus = _fresh_bus()
    bus.emit_model_fallback(
        "u7",
        run_id="run-9",
        from_model="nvidia/nemotron-3-nano-30b-a3b",
        to_model="nvidia/nemotron-3-super-120b-a12b",
        reason="model unavailable: 404",
    )
    ev = _only(bus, "u7")
    assert set(ev) == {"type", "run_id", "from_model", "to_model", "reason"}
    assert ev["type"] == "model_fallback"
    assert ev["from_model"] == "nvidia/nemotron-3-nano-30b-a3b"
    assert ev["to_model"] == "nvidia/nemotron-3-super-120b-a12b"
    assert ev["reason"] == "model unavailable: 404"
    _assert_body_free(ev)


# ── isolation: an event-bus failure never kills a run ─────────────────────────


@pytest.mark.parametrize(
    "call",
    [
        lambda bus: bus.emit_thread_classified(
            "boom",
            run_id="r",
            item_id="i",
            subject="s",
            from_email="f@e.com",
            category="c",
            action="keep",
            decided_by="rule",
            confidence=0.5,
            reasoning="why",
            review_state="provisional",
        ),
        lambda bus: bus.emit_provider_degraded(
            "boom", run_id="r", provider="nvidia", model="m", calls=1, retries=1,
            consecutive_failures=1,
        ),
        lambda bus: bus.emit_run_resumable(
            "boom", run_id="r", items_total=1, items_decided=0, reason="interrupted"
        ),
        lambda bus: bus.emit_model_fallback(
            "boom", run_id="r", from_model="a", to_model="b", reason="why"
        ),
    ],
)
def test_helper_never_propagates_a_raising_subscriber(monkeypatch, call):
    bus = _fresh_bus()

    def _explode(*_a, **_kw):
        raise RuntimeError("subscriber blew up")

    monkeypatch.setattr(bus, "emit", _explode)
    call(bus)  # must not raise


def test_bad_argument_types_do_not_propagate():
    """A caller passing a non-numeric count must not take the run down."""
    bus = _fresh_bus()
    bus.emit_provider_degraded(
        "u8",
        run_id="r",
        provider="nvidia",
        model="m",
        calls="not-a-number",  # type: ignore[arg-type]
        retries=1,
        consecutive_failures=0,
    )
    assert bus.replay_buffer("u8") == []
