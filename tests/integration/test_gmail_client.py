"""Gmail client integration (auth-gmail slice): INBOX-only reads + mini-audit.

Drives the real ``GmailAdapter`` / ``collect_inbox_snapshot`` / normalisation
code against a fake transport (a live googleapiclient request from a test is
refused suite-wide by ``assert_not_real_gmail_request``). Asserts the three
spec-critical properties: INBOX-only newest-first reads, metadata-only privacy,
and a strictly read-only audit.
"""

from __future__ import annotations

import pytest

from channels.base import ReauthRequired
from channels.gmail.adapter import GmailAdapter
from channels.gmail.audit import collect_inbox_snapshot
from tests.gmail_slice_fakes import FakeGmailService, make_thread


def _adapter(service) -> GmailAdapter:
    return GmailAdapter(service, user_id="test-user-gmail", redactor=lambda s: s)


def _inbox_service() -> FakeGmailService:
    threads = {
        "t-new": make_thread(
            "t-new",
            sender="GitHub <notifications@github.com>",
            subject="PR merged",
            internal_ms=1_700_000_300_000,
            list_unsubscribe="<https://github.com/unsub>",
        ),
        "t-mid": make_thread(
            "t-mid",
            sender="Chase <alerts@chase.com>",
            subject="Statement ready",
            internal_ms=1_700_000_200_000,
            labels=["INBOX", "CATEGORY_UPDATES"],
        ),
        "t-old": make_thread(
            "t-old",
            sender="GitHub <notifications@github.com>",
            subject="Issue closed",
            internal_ms=1_700_000_100_000,
        ),
    }
    return FakeGmailService(threads)


# --- happy path: INBOX reads -------------------------------------------------


def test_list_threads_is_inbox_only_metadata_only_newest_first():
    service = _inbox_service()
    items = _adapter(service).list_threads(limit=10)

    # Every listing is scoped in:inbox (covers all category tabs); archived mail
    # is unreachable from this code path.
    for kwargs in service.calls_named("threads.list"):
        assert kwargs["q"].startswith("in:inbox")
    # Per-thread fetches are metadata format — bodies are never requested.
    for kwargs in service.calls_named("threads.get"):
        assert kwargs["format"] == "metadata"

    assert [i.external_thread_id for i in items] == ["t-new", "t-mid", "t-old"]
    first = items[0]
    assert first.from_email == "notifications@github.com"
    assert first.subject == "PR merged"
    assert first.unsubscribe_url == "https://github.com/unsub"
    assert first.is_unread is True
    assert first.snippet_redacted  # headers + snippet + signals, nothing more
    assert "CATEGORY_UPDATES" in items[1].channel_labels  # Gmail tab signal


def test_mini_audit_snapshot_counts_senders_and_is_strictly_read_only():
    def estimates(query: str) -> int:
        if query == "in:inbox":
            return 250
        if query == "in:inbox is:unread":
            return 41
        if query.startswith("in:inbox category:"):
            return {"promotions": 120, "updates": 80}.get(query.rsplit(":", 1)[1], 0)
        if query.startswith("in:inbox older_than:"):
            days = int(query.rsplit(":", 1)[1].rstrip("d"))
            return 5 if days <= 30 else 0
        return 0

    service = FakeGmailService(_inbox_service().threads_data, estimates=estimates)
    snapshot = collect_inbox_snapshot(_adapter(service), sample_size=10)

    assert snapshot["total_inbox_threads"] == 250
    assert snapshot["unread"] == 41
    assert snapshot["oldest_days"] == 30
    assert snapshot["category_tab_counts"]["promotions"] == 120
    assert snapshot["approximate"] is True
    assert snapshot["top_senders"][0] == {
        "address": "notifications@github.com",
        "count": 2,
    }

    # Provably zero mutations: no write method was ever invoked.
    write_calls = [m for m, _ in service.calls if m not in ("threads.list", "threads.get")]
    assert write_calls == []


# --- edge: empty inbox -------------------------------------------------------


def test_empty_inbox_yields_empty_audit_not_an_error():
    service = FakeGmailService({}, estimates=lambda q: 0)
    adapter = _adapter(service)
    assert adapter.list_threads(limit=10) == []
    snapshot = collect_inbox_snapshot(adapter, sample_size=10)
    assert snapshot["total_inbox_threads"] == 0
    assert snapshot["oldest_days"] == 0
    assert snapshot["top_senders"] == []


# --- error path: revoked token ----------------------------------------------


def test_revoked_token_surfaces_as_reauth_required_not_a_traceback():
    from google.auth.exceptions import RefreshError

    class RevokedService(FakeGmailService):
        def list(self, **kwargs):
            def raise_refresh():
                raise RefreshError("invalid_grant: Token has been revoked.")
            return type("R", (), {"execute": staticmethod(raise_refresh)})()

    with pytest.raises(ReauthRequired) as excinfo:
        _adapter(RevokedService()).list_threads(limit=5)
    assert "reconnect Gmail" in str(excinfo.value)
