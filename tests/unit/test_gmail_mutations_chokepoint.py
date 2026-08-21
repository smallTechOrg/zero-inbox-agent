"""The four-op Gmail mutation choke point (auth-gmail slice).

spec/architecture.md: (a) audit row before the Gmail call, (b) only the four
reversible ops are representable, (c) the test-isolation flag makes the write a
recorded no-op.
"""

from __future__ import annotations

import httplib2
import pytest
from googleapiclient.errors import HttpError

from channels.base import RateLimited, ReauthRequired
from channels.gmail.mutations import (
    ALLOWED_MUTATIONS,
    INVERSE_MUTATION,
    WRITE_DISABLED_ENV,
    ForbiddenMutation,
    GmailMutator,
)
from tests.gmail_slice_fakes import FakeGmailService, make_thread


@pytest.fixture(autouse=True)
def _exercise_the_real_write_path(monkeypatch):
    """These tests drive the write path against FakeGmailService (never live
    Gmail), so clear BOTH isolation flags — conftest exports
    AGENT_TEST_ISOLATION=1 globally, which gmail_writes_disabled() honours."""
    monkeypatch.delenv("AGENT_TEST_ISOLATION", raising=False)
    monkeypatch.delenv(WRITE_DISABLED_ENV, raising=False)


def _mutator(service, **kw) -> GmailMutator:
    return GmailMutator(service, sleep=lambda _s: None, **kw)


# --- happy paths -----------------------------------------------------------


def test_add_label_sends_one_atomic_modify():
    service = FakeGmailService({"t1": make_thread("t1")})
    result = _mutator(service).apply("add_label", "t1", label_id="Label_1")

    modify = service.calls_named("threads.modify")
    assert len(modify) == 1
    assert modify[0]["id"] == "t1"
    assert modify[0]["body"] == {"addLabelIds": ["Label_1"], "removeLabelIds": []}
    assert result["applied"] is True and result["simulated"] is False


@pytest.mark.parametrize(
    ("op", "add", "remove"),
    [
        ("remove_label", [], ["Label_1"]),
        ("remove_inbox", [], ["INBOX"]),
        ("restore_inbox", ["INBOX"], []),
    ],
)
def test_each_op_maps_to_exact_label_change(op, add, remove):
    service = FakeGmailService({"t1": make_thread("t1")})
    _mutator(service).apply(op, "t1", label_id="Label_1")
    body = service.calls_named("threads.modify")[0]["body"]
    assert body == {"addLabelIds": add, "removeLabelIds": remove}


def test_audit_row_is_written_before_gmail_is_called():
    order: list[str] = []

    class OrderedService(FakeGmailService):
        def modify(self, **kwargs):
            order.append("gmail")
            return super().modify(**kwargs)

    service = OrderedService({"t1": make_thread("t1")})
    mutator = _mutator(
        service, audit_writer=lambda record: order.append(f"audit:{record['op']}")
    )
    mutator.apply("remove_inbox", "t1", reason="Newsletters rule")
    assert order == ["audit:remove_inbox", "gmail"]


def test_inverse_map_is_a_perfect_involution():
    assert set(INVERSE_MUTATION) == set(ALLOWED_MUTATIONS)
    for op, inverse in INVERSE_MUTATION.items():
        assert INVERSE_MUTATION[inverse] == op
        assert inverse in ALLOWED_MUTATIONS


def test_apply_inverse_issues_the_inverse_op():
    service = FakeGmailService({"t1": make_thread("t1")})
    _mutator(service).apply_inverse("remove_inbox", "t1")
    body = service.calls_named("threads.modify")[0]["body"]
    assert body == {"addLabelIds": ["INBOX"], "removeLabelIds": []}


# --- test-isolation guard ---------------------------------------------------


def test_write_disabled_flag_records_audit_but_never_calls_gmail(monkeypatch):
    monkeypatch.setenv(WRITE_DISABLED_ENV, "1")
    audit: list[dict] = []
    service = FakeGmailService({"t1": make_thread("t1")})
    result = _mutator(service, audit_writer=audit.append).apply(
        "add_label", "t1", label_id="Label_1"
    )
    assert result["applied"] is True and result["simulated"] is True
    assert audit and audit[0]["gmail_thread_id"] == "t1"
    assert service.calls_named("threads.modify") == []


# --- error paths ------------------------------------------------------------


@pytest.mark.parametrize("op", ["delete", "trash", "spam", "archive", "create_filter", ""])
def test_anything_outside_the_four_ops_is_refused(op):
    service = FakeGmailService()
    with pytest.raises(ForbiddenMutation):
        _mutator(service).apply(op, "t1", label_id="Label_1")
    assert service.calls == []  # refused before any request is even built


@pytest.mark.parametrize("op", ["add_label", "remove_label"])
def test_label_ops_require_a_label_id(op):
    with pytest.raises(ForbiddenMutation):
        _mutator(FakeGmailService()).apply(op, "t1")


def test_missing_thread_id_is_refused():
    with pytest.raises(ForbiddenMutation):
        _mutator(FakeGmailService()).apply("remove_inbox", "")


def _http_error(status: int) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), b"boom")


def test_revoked_token_is_reauth_required_never_a_traceback():
    class RevokedService(FakeGmailService):
        def modify(self, **kwargs):
            self.calls.append(("threads.modify", kwargs))
            def raise_401():
                raise _http_error(401)
            return type("R", (), {"execute": staticmethod(raise_401)})()

    with pytest.raises(ReauthRequired) as excinfo:
        _mutator(RevokedService()).apply("remove_inbox", "t1")
    assert "reconnect Gmail" in str(excinfo.value)


def test_google_refresh_error_maps_to_reauth_required():
    from google.auth.exceptions import RefreshError

    class ExpiredService(FakeGmailService):
        def modify(self, **kwargs):
            def raise_refresh():
                raise RefreshError("invalid_grant: Token has been expired or revoked.")
            return type("R", (), {"execute": staticmethod(raise_refresh)})()

    with pytest.raises(ReauthRequired):
        _mutator(ExpiredService()).apply("restore_inbox", "t1")


def test_persistent_429_backs_off_then_rate_limited():
    sleeps: list[float] = []

    class BusyService(FakeGmailService):
        def modify(self, **kwargs):
            def raise_429():
                raise _http_error(429)
            return type("R", (), {"execute": staticmethod(raise_429)})()

    mutator = GmailMutator(BusyService(), sleep=sleeps.append)
    with pytest.raises(RateLimited):
        mutator.apply("add_label", "t1", label_id="L")
    assert len(sleeps) == 3  # backed off every attempt
