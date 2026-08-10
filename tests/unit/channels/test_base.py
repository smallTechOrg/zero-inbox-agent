"""Unit tests for the channel-agnostic adapter interface."""

from datetime import datetime, timezone

import pytest


ITEMS_TABLE_FIELDS = {
    "external_thread_id",
    "external_message_ids",
    "subject",
    "from_name",
    "from_email",
    "from_domain",
    "to_emails",
    "cc_emails",
    "list_id",
    "unsubscribe_url",
    "message_count",
    "has_attachments",
    "snippet_redacted",
    "internal_date",
    "is_unread",
    "channel_labels",
}


def _minimal_item(**overrides):
    from channels.base import ChannelItem

    payload = {
        "external_thread_id": "t1",
        "external_message_ids": ["m1"],
        "subject": "Hello",
        "from_name": "Ada",
        "from_email": "ada@example.com",
        "from_domain": "example.com",
        "to_emails": ["me@example.com"],
        "cc_emails": [],
        "list_id": None,
        "unsubscribe_url": None,
        "message_count": 1,
        "has_attachments": False,
        "snippet_redacted": "hi",
        "internal_date": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "is_unread": True,
        "channel_labels": ["INBOX"],
    }
    payload.update(overrides)
    return ChannelItem(**payload)


def test_channel_item_fields_match_the_items_table_contract():
    from channels.base import ChannelItem

    assert set(ChannelItem.model_fields) == ITEMS_TABLE_FIELDS


def test_channel_item_has_no_body_field():
    from channels.base import ChannelItem

    names = {f.lower() for f in ChannelItem.model_fields}
    assert not {n for n in names if "body" in n or n in {"text", "html", "content"}}


def test_snippet_longer_than_200_chars_is_truncated():
    item = _minimal_item(snippet_redacted="x" * 500)

    assert len(item.snippet_redacted) == 200


def test_channel_item_serialises_to_the_items_column_names():
    item = _minimal_item()

    # `id` is computed, not a declared field: the graph and persistence layer key
    # every item on it, and it maps to the `items.id` column.
    assert set(item.model_dump()) == ITEMS_TABLE_FIELDS | {"id"}


def test_channel_item_id_is_the_key_the_triage_graph_uses():
    """Regression: adapters exposed only `external_thread_id` while the graph and
    the LLM verdict map addressed `item["id"]`, so every run died with KeyError."""
    item = _minimal_item()

    assert item.id == item.external_thread_id
    assert item.model_dump()["id"] == item.external_thread_id


def test_channel_adapter_cannot_be_instantiated_directly():
    from channels.base import ChannelAdapter

    with pytest.raises(TypeError):
        ChannelAdapter()


def test_channel_adapter_declares_the_full_read_and_mutation_surface():
    from channels.base import ChannelAdapter

    expected = {
        "account_email",
        "list_threads",
        "fetch_thread_body",
        "sender_history",
        "list_labels",
        "archive_and_label",
        "undo_archive_and_label",
        "create_label",
        "create_filter",
        "create_draft",
    }
    assert expected <= set(ChannelAdapter.__abstractmethods__)


def test_channel_adapter_has_archive_and_label_not_the_old_three_stubs():
    """Regression: spec requires archive_and_label / undo_archive_and_label exactly."""
    from channels.base import ChannelAdapter

    abstract = set(ChannelAdapter.__abstractmethods__)
    assert "archive_and_label" in abstract
    assert "undo_archive_and_label" in abstract
    # Old methods must not exist on the interface.
    assert "archive_thread" not in abstract
    assert "add_labels" not in abstract
    assert "remove_labels" not in abstract


def test_dry_run_violation_and_reauth_required_are_distinct_errors():
    from channels.base import ChannelError, DryRunViolation, ReauthRequired

    assert issubclass(DryRunViolation, ChannelError)
    assert issubclass(ReauthRequired, ChannelError)
    assert not issubclass(DryRunViolation, ReauthRequired)


def test_sender_signal_carries_the_ever_replied_never_miss_flag():
    from channels.base import SenderSignal

    signal = SenderSignal(sender_email="ada@example.com")

    assert signal.ever_replied is False
    assert signal.replied_count == 0
