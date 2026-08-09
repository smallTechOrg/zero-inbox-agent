"""Unit tests for Gmail thread -> ChannelItem normalization."""

from datetime import timezone

import pytest


def _message(
    mid: str,
    *,
    headers: dict[str, str] | None = None,
    snippet: str = "a snippet",
    internal_date: str = "1767225600000",  # 2026-01-01T00:00:00Z
    label_ids: list[str] | None = None,
    parts: list[dict] | None = None,
):
    base = {
        "From": "Ada Lovelace <ada@Example.COM>",
        "To": "me@mine.com",
        "Subject": "Quarterly numbers",
        "Date": "Thu, 1 Jan 2026 00:00:00 +0000",
    }
    base.update(headers or {})
    payload = {"headers": [{"name": k, "value": v} for k, v in base.items()]}
    if parts is not None:
        payload["parts"] = parts
    return {
        "id": mid,
        "snippet": snippet,
        "internalDate": internal_date,
        "labelIds": label_ids if label_ids is not None else ["INBOX", "UNREAD"],
        "payload": payload,
    }


def _thread(messages):
    return {"id": "thread-1", "messages": messages}


def test_a_five_message_thread_becomes_exactly_one_item_with_message_count_five():
    from channels.gmail.normalize import normalize_thread

    item = normalize_thread(_thread([_message(f"m{i}") for i in range(5)]))

    assert item.external_thread_id == "thread-1"
    assert item.message_count == 5
    assert item.external_message_ids == ["m0", "m1", "m2", "m3", "m4"]


def test_the_latest_message_supplies_sender_subject_and_date():
    from channels.gmail.normalize import normalize_thread

    item = normalize_thread(
        _thread(
            [
                _message("m1", headers={"From": "Old <old@a.com>", "Subject": "First"}),
                _message(
                    "m2",
                    headers={"From": "New <new@b.com>", "Subject": "Re: First"},
                    internal_date="1767312000000",
                ),
            ]
        )
    )

    assert item.from_email == "new@b.com"
    assert item.subject == "Re: First"
    assert item.internal_date.astimezone(timezone.utc).day == 2


def test_sender_email_and_domain_are_lowercased_and_split():
    from channels.gmail.normalize import normalize_thread

    item = normalize_thread(_thread([_message("m1")]))

    assert item.from_name == "Ada Lovelace"
    assert item.from_email == "ada@example.com"
    assert item.from_domain == "example.com"


def test_list_id_and_unsubscribe_url_are_extracted():
    from channels.gmail.normalize import normalize_thread

    item = normalize_thread(
        _thread(
            [
                _message(
                    "m1",
                    headers={
                        "List-Id": "Substack Weekly <weekly.substack.com>",
                        "List-Unsubscribe": "<mailto:x@y.com>, <https://sub.example/unsub?a=1>",
                    },
                )
            ]
        )
    )

    assert item.list_id == "weekly.substack.com"
    assert item.unsubscribe_url == "https://sub.example/unsub?a=1"


def test_unread_and_labels_come_from_the_union_of_thread_labels():
    from channels.gmail.normalize import normalize_thread

    unread = normalize_thread(_thread([_message("m1", label_ids=["INBOX", "UNREAD"])]))
    read = normalize_thread(_thread([_message("m1", label_ids=["INBOX"])]))

    assert unread.is_unread is True
    assert "INBOX" in unread.channel_labels
    assert read.is_unread is False


def test_attachment_presence_is_detected_from_named_parts():
    from channels.gmail.normalize import normalize_thread

    with_attach = normalize_thread(
        _thread([_message("m1", parts=[{"filename": ""}, {"filename": "invoice.pdf"}])])
    )
    without = normalize_thread(_thread([_message("m1", parts=[{"filename": ""}])]))

    assert with_attach.has_attachments is True
    assert without.has_attachments is False


def test_snippet_is_truncated_to_200_characters():
    from channels.gmail.normalize import normalize_thread

    item = normalize_thread(_thread([_message("m1", snippet="y" * 900)]))

    assert len(item.snippet_redacted) == 200


def test_snippet_is_passed_through_the_injected_redactor_before_it_is_returned():
    from channels.gmail.normalize import normalize_thread

    item = normalize_thread(
        _thread([_message("m1", snippet="key sk-abc123")]),
        redactor=lambda s: s.replace("sk-abc123", "[REDACTED:api_key]"),
    )

    assert item.snippet_redacted == "key [REDACTED:api_key]"
    assert "sk-abc123" not in item.snippet_redacted


# --- edge cases ---------------------------------------------------------


def test_a_bare_address_with_no_display_name_normalises():
    from channels.gmail.normalize import normalize_thread

    item = normalize_thread(_thread([_message("m1", headers={"From": "solo@x.io"})]))

    assert item.from_name == ""
    assert item.from_email == "solo@x.io"
    assert item.from_domain == "x.io"


def test_missing_subject_and_from_headers_yield_empty_strings_not_a_crash():
    from channels.gmail.normalize import normalize_thread

    message = _message("m1")
    message["payload"]["headers"] = []

    item = normalize_thread(_thread([message]))

    assert item.subject == ""
    assert item.from_email == ""
    assert item.from_domain == ""


def test_multiple_to_and_cc_addresses_are_split_into_lists():
    from channels.gmail.normalize import normalize_thread

    item = normalize_thread(
        _thread(
            [
                _message(
                    "m1",
                    headers={
                        "To": "A <a@x.com>, b@y.com",
                        "Cc": "c@z.com",
                    },
                )
            ]
        )
    )

    assert item.to_emails == ["a@x.com", "b@y.com"]
    assert item.cc_emails == ["c@z.com"]


def test_a_malformed_internal_date_falls_back_without_crashing():
    from channels.gmail.normalize import normalize_thread

    message = _message("m1", internal_date="not-a-number")

    item = normalize_thread(_thread([message]))

    assert item.internal_date.tzinfo is not None


# --- error paths --------------------------------------------------------


def test_a_thread_with_no_messages_is_rejected():
    from channels.base import ChannelError
    from channels.gmail.normalize import normalize_thread

    with pytest.raises(ChannelError):
        normalize_thread({"id": "thread-1", "messages": []})


def test_a_thread_with_no_id_is_rejected():
    from channels.base import ChannelError
    from channels.gmail.normalize import normalize_thread

    with pytest.raises(ChannelError):
        normalize_thread({"messages": [_message("m1")]})


def test_normalized_item_never_carries_body_data():
    from channels.gmail.normalize import normalize_thread

    message = _message("m1")
    message["payload"]["body"] = {"data": "U0VDUkVUIEJPRFk="}

    item = normalize_thread(_thread([message]))

    assert "U0VDUkVUIEJPRFk=" not in str(item.model_dump())
