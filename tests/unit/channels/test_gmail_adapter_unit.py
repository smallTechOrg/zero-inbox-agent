"""Unit tests for the Gmail adapter (read-only in Phase 1)."""

import pytest

from tests.unit.channels.fake_gmail import FakeGmailService, http_error


def _msg(mid, *, frm="Ada <ada@example.com>", subject="Hi", to="me@mine.com", labels=None):
    return {
        "id": mid,
        "snippet": "snippet text",
        "internalDate": "1767225600000",
        "labelIds": labels if labels is not None else ["INBOX", "UNREAD"],
        "payload": {
            "headers": [
                {"name": "From", "value": frm},
                {"name": "To", "value": to},
                {"name": "Subject", "value": subject},
            ]
        },
    }


def _thread(tid, messages):
    return {"id": tid, "messages": messages}


def _adapter(service, **kwargs):
    from channels.gmail.adapter import GmailAdapter

    return GmailAdapter(
        service,
        user_id=kwargs.pop("user_id", "user-1"),
        account_email=kwargs.pop("account_email", "user@example.com"),
        **kwargs,
    )


# --- happy path ---------------------------------------------------------


def test_list_threads_returns_normalized_items_for_each_inbox_thread():
    service = FakeGmailService(
        threads={
            "t1": _thread("t1", [_msg("m1", subject="Invoice")]),
            "t2": _thread("t2", [_msg("m2", subject="Newsletter"), _msg("m3")]),
        }
    )

    items = _adapter(service).list_threads(limit=200)

    assert [i.external_thread_id for i in items] == ["t1", "t2"]
    assert items[0].subject == "Invoice"
    assert items[1].message_count == 2


def test_list_threads_requests_only_inbox_metadata_never_full_bodies():
    service = FakeGmailService(threads={"t1": _thread("t1", [_msg("m1")])})

    _adapter(service).list_threads(limit=10)

    assert service.thread_list_calls[0]["labelIds"] == ["INBOX"]
    assert all(call["format"] == "metadata" for call in service.thread_get_calls)


def test_list_threads_paginates_until_the_limit_is_reached():
    threads = {f"t{i}": _thread(f"t{i}", [_msg(f"m{i}")]) for i in range(150)}
    pages = [
        {"threads": [{"id": f"t{i}"} for i in range(100)], "nextPageToken": "p2"},
        {"threads": [{"id": f"t{i}"} for i in range(100, 150)]},
    ]
    service = FakeGmailService(threads=threads, thread_pages=pages)

    items = _adapter(service).list_threads(limit=150)

    assert len(items) == 150
    assert service.thread_list_calls[1]["pageToken"] == "p2"


def test_list_threads_never_returns_more_than_the_limit():
    threads = {f"t{i}": _thread(f"t{i}", [_msg(f"m{i}")]) for i in range(30)}
    service = FakeGmailService(threads=threads)

    items = _adapter(service).list_threads(limit=5)

    assert len(items) == 5


def test_sender_history_marks_every_address_the_user_has_replied_to():
    service = FakeGmailService(
        sent_messages=[
            _msg("s1", frm="me@mine.com", to="Ada <ada@example.com>", labels=["SENT"]),
            _msg("s2", frm="me@mine.com", to="ada@example.com, bob@other.com", labels=["SENT"]),
        ]
    )

    history = _adapter(service).sender_history(limit=100)

    assert history["ada@example.com"].ever_replied is True
    assert history["ada@example.com"].replied_count == 2
    assert history["bob@other.com"].replied_count == 1


def test_account_email_comes_from_the_gmail_profile():
    service = FakeGmailService(account_email="real@gmail.com")

    assert _adapter(service, account_email="").account_email() == "real@gmail.com"


def test_list_labels_returns_id_and_name_pairs():
    service = FakeGmailService(labels=[{"id": "L1", "name": "ZeroInbox/News"}])

    labels = _adapter(service).list_labels()

    assert labels == [{"id": "L1", "name": "ZeroInbox/News"}]


# --- edge cases ---------------------------------------------------------


def test_an_empty_inbox_yields_an_empty_list_not_an_error():
    service = FakeGmailService(threads={}, thread_pages=[{}])

    assert _adapter(service).list_threads(limit=200) == []


def test_a_limit_of_zero_makes_no_gmail_calls():
    service = FakeGmailService(threads={"t1": _thread("t1", [_msg("m1")])})

    items = _adapter(service).list_threads(limit=0)

    assert items == []
    assert service.thread_list_calls == []


def test_a_thread_that_fails_to_fetch_is_skipped_rather_than_failing_the_whole_listing():
    service = FakeGmailService(
        threads={
            "t1": _thread("t1", [_msg("m1")]),
            "t2": _thread("t2", [_msg("m2")]),
        }
    )
    service.thread_errors["t1"] = http_error(404, "not found")

    items = _adapter(service).list_threads(limit=200)

    assert [i.external_thread_id for i in items] == ["t2"]


def test_sender_history_on_an_empty_sent_folder_returns_an_empty_mapping():
    assert _adapter(FakeGmailService()).sender_history(limit=100) == {}


def test_a_negative_limit_is_rejected():
    from channels.base import ChannelError

    with pytest.raises(ChannelError):
        _adapter(FakeGmailService()).list_threads(limit=-1)


# --- error paths --------------------------------------------------------


def test_a_401_from_gmail_raises_reauth_required():
    from channels.base import ReauthRequired

    service = FakeGmailService()
    service.profile_errors = [http_error(401, "invalid credentials")]

    with pytest.raises(ReauthRequired):
        _adapter(service).account_email()


def test_a_transient_429_is_retried_and_then_succeeds():
    from channels.base import ReauthRequired  # noqa: F401  (import sanity)

    service = FakeGmailService(account_email="real@gmail.com")
    service.profile_errors = [http_error(429, "rate limited")]

    assert _adapter(service, backoff_seconds=0).account_email() == "real@gmail.com"


def test_a_persistent_429_eventually_raises_rate_limited():
    from channels.base import RateLimited

    service = FakeGmailService()
    service.profile_errors = [http_error(429, "rate limited") for _ in range(5)]

    with pytest.raises(RateLimited):
        _adapter(service, backoff_seconds=0).account_email()


# --- Phase 1 dry-run guarantee -----------------------------------------


@pytest.mark.parametrize(
    "method,args",
    [
        ("archive_thread", ("t1",)),
        ("add_labels", ("t1", ["L1"])),
        ("remove_labels", ("t1", ["L1"])),
        ("create_label", ("ZeroInbox/News",)),
        ("create_filter", ({"from": "a@b.com"}, {"addLabelIds": ["L1"]})),
        ("create_draft", ("t1", "body text")),
    ],
)
def test_every_mutation_method_exists_but_raises_dry_run_violation_in_phase_1(method, args):
    from channels.base import DryRunViolation

    adapter = _adapter(FakeGmailService())

    with pytest.raises(DryRunViolation):
        getattr(adapter, method)(*args)


def test_the_adapter_exposes_no_delete_or_trash_operation_ever():
    from channels.gmail.adapter import GmailAdapter

    names = {n.lower() for n in dir(GmailAdapter) if not n.startswith("_")}

    assert not {n for n in names if "delete" in n or "trash" in n or "spam" in n}


def test_listed_items_never_carry_a_body_attribute():
    service = FakeGmailService(threads={"t1": _thread("t1", [_msg("m1")])})

    items = _adapter(service).list_threads(limit=1)

    assert not hasattr(items[0], "body")
