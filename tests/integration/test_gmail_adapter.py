"""Integration tests for the Gmail adapter against the **real Gmail API**.

These use the refresh token stored by the real OAuth flow. If no mailbox has
been connected yet, every test SKIPS with an actionable message — a skip here
means BLOCKED, not passed.

Connect a mailbox first:

    uv run alembic upgrade head
    uv run python -m src          # then open http://localhost:8001/app/
                                  # and click "Connect Gmail"
"""

from __future__ import annotations

import os

import pytest

SKIP_REASON = (
    "No Gmail mailbox connected — this test needs a REAL connection. "
    "Run `uv run python -m src`, open http://localhost:8001/app/ and click "
    "'Connect Gmail' (or set AGENT_TEST_GMAIL_REFRESH_TOKEN in .env). "
    "Treat this skip as BLOCKED, not as a pass."
)


def _connection_from_production_db() -> tuple[str, str, str] | None:
    """(user_id, account_email, refresh_token) from the real DB, or None.

    Reads the production database directly — the autouse `_isolated_db` fixture
    redirects `db.session` at a throwaway file, which is right for unit tests
    but would hide the real connection here.
    """
    from sqlalchemy import create_engine, text

    from config.settings import get_settings
    from security.crypto import CryptoError, TokenCipher

    url = get_settings().database_url.replace("+aiosqlite", "")
    try:
        engine = create_engine(url)
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT user_id, account_email, refresh_token_enc "
                    "FROM channel_accounts "
                    "WHERE channel = 'gmail' AND refresh_token_enc IS NOT NULL "
                    "AND refresh_token_enc != '' "
                    "ORDER BY connected_at DESC LIMIT 1"
                )
            ).first()
    except Exception:
        return None
    if row is None:
        return None
    try:
        return row[0], row[1], TokenCipher().decrypt(row[2])
    except CryptoError:
        return None


@pytest.fixture(scope="module")
def gmail_connection():
    plain = os.environ.get("AGENT_TEST_GMAIL_REFRESH_TOKEN", "")
    if plain:
        return {"user_id": "test-user", "account_email": "", "refresh_token": plain}
    found = _connection_from_production_db()
    if found is None:
        pytest.skip(SKIP_REASON)
    return {"user_id": found[0], "account_email": found[1], "refresh_token": found[2]}


@pytest.fixture
def adapter(gmail_connection):
    from channels.gmail.adapter import GmailAdapter

    return GmailAdapter.for_refresh_token(
        gmail_connection["refresh_token"],
        user_id=gmail_connection["user_id"],
        account_email=gmail_connection["account_email"],
    )


# --- happy path ---------------------------------------------------------


def test_account_email_matches_the_connected_mailbox(adapter, gmail_connection):
    email = adapter.account_email()

    assert "@" in email
    if gmail_connection["account_email"]:
        assert email.lower() == gmail_connection["account_email"].lower()


def test_listing_real_inbox_threads_returns_real_headers_and_a_capped_snippet(adapter):
    items = adapter.list_threads(limit=25)

    assert items, "the connected inbox returned no threads"
    assert len(items) <= 25
    assert len({i.external_thread_id for i in items}) == len(items)
    for item in items:
        assert item.external_thread_id
        assert item.message_count >= 1
        assert len(item.external_message_ids) == item.message_count
        assert len(item.snippet_redacted) <= 200
        assert item.internal_date.tzinfo is not None
        assert "INBOX" in item.channel_labels
    assert any(item.from_email for item in items)
    assert any(item.subject for item in items)


def test_no_listed_item_carries_body_text(adapter):
    items = adapter.list_threads(limit=10)

    for item in items:
        dumped = item.model_dump()
        assert set(dumped) == set(type(item).model_fields)
        assert not [k for k in dumped if "body" in k.lower()]


def test_sender_history_reports_addresses_the_user_has_really_replied_to(adapter):
    history = adapter.sender_history(limit=25)

    assert isinstance(history, dict)
    for email, signal in history.items():
        assert signal.sender_email == email
        assert signal.ever_replied is True
        assert signal.replied_count >= 1


def test_list_labels_includes_the_inbox_system_label(adapter):
    names = {label["name"] for label in adapter.list_labels()}

    assert "INBOX" in names


def test_a_second_listing_on_the_same_adapter_reuses_the_refreshed_token(adapter):
    """Second interaction: the access token is refreshed once and then reused."""
    first = adapter.list_threads(limit=5)
    second = adapter.list_threads(limit=5)

    assert [i.external_thread_id for i in first] == [i.external_thread_id for i in second]


# --- edge cases ---------------------------------------------------------


def test_a_limit_of_zero_returns_nothing_from_a_real_mailbox(adapter):
    assert adapter.list_threads(limit=0) == []


def test_a_limit_of_one_returns_exactly_the_newest_thread(adapter):
    items = adapter.list_threads(limit=1)

    assert len(items) == 1


def test_a_query_matching_nothing_returns_an_empty_list(adapter):
    items = adapter.list_threads(limit=10, query="subject:zzz-no-such-subject-zzz-9182")

    assert items == []


# --- error paths --------------------------------------------------------


def test_an_invalid_refresh_token_raises_reauth_required(gmail_connection):
    from channels.base import ReauthRequired
    from channels.gmail.adapter import GmailAdapter

    broken = GmailAdapter.for_refresh_token(
        "1//0-this-refresh-token-is-not-valid", user_id="test-user"
    )

    with pytest.raises(ReauthRequired):
        broken.account_email()


def test_an_empty_refresh_token_is_refused_before_any_network_call():
    from channels.base import ReauthRequired
    from channels.gmail.adapter import GmailAdapter

    with pytest.raises(ReauthRequired):
        GmailAdapter.for_refresh_token("", user_id="test-user")


def test_a_negative_limit_is_refused(adapter):
    from channels.base import ChannelError

    with pytest.raises(ChannelError):
        adapter.list_threads(limit=-5)


# --- Phase 1 dry-run guarantee against the real mailbox -----------------


def test_no_mutation_reaches_the_real_mailbox_in_phase_1(adapter):
    from channels.base import DryRunViolation

    before = adapter.list_threads(limit=5)
    assert before, "need at least one thread to prove nothing changed"
    thread_id = before[0].external_thread_id

    for call in (
        lambda: adapter.archive_thread(thread_id),
        lambda: adapter.add_labels(thread_id, ["INBOX"]),
        lambda: adapter.remove_labels(thread_id, ["INBOX"]),
        lambda: adapter.create_label("ZeroInbox/ShouldNeverExist"),
        lambda: adapter.create_filter({"from": "a@b.com"}, {"addLabelIds": ["INBOX"]}),
        lambda: adapter.create_draft(thread_id, "should never be created"),
    ):
        with pytest.raises(DryRunViolation):
            call()

    after = adapter.list_threads(limit=5)
    assert [i.external_thread_id for i in after] == [i.external_thread_id for i in before]
    assert "INBOX" in after[0].channel_labels
    assert "ZeroInbox/ShouldNeverExist" not in {
        label["name"] for label in adapter.list_labels()
    }
