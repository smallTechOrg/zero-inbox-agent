"""The user is not his own correspondent — layer 1, at the harvest.

Live defect this pins: ``_accumulate_recipients`` harvested ``To``/``Cc`` from
the user's own ``SENT`` mail, so ``psykrsna@gmail.com`` became his own
most-replied-to address (*"24 replies of 0 received"*) and 18 threads were held
in the inbox by the never-miss reply-history guard.

Fails against pre-Phase-9 code: the old harvest produced a signal for all three
spellings.
"""

import pytest

from tests.unit.channels.fake_gmail import FakeGmailService, _Request, _Users, http_error

ACCOUNT = "psykrsna@gmail.com"


def _sent(mid, to, *, cc=""):
    headers = [
        {"name": "From", "value": ACCOUNT},
        {"name": "To", "value": to},
        {"name": "Date", "value": "Wed, 1 Jan 2026 00:00:00 +0000"},
    ]
    if cc:
        headers.append({"name": "Cc", "value": cc})
    return {
        "id": mid,
        "snippet": "",
        "internalDate": "1767225600000",
        "labelIds": ["SENT"],
        "payload": {"headers": headers},
    }


class _SendAs:
    def __init__(self, service):
        self._s = service

    def list(self, *, userId):
        self._s.send_as_calls += 1
        if self._s.send_as_error is not None:
            raise self._s.send_as_error
        return _Request({"sendAs": [{"sendAsEmail": a} for a in self._s.send_as]})


class _Settings:
    def __init__(self, service):
        self._s = service

    def sendAs(self):
        return _SendAs(self._s)


class _UsersWithSettings(_Users):
    def settings(self):
        return _Settings(self._s)


class SendAsGmailService(FakeGmailService):
    """The fake, plus the ``users.settings.sendAs.list`` surface Phase 9 reads."""

    def __init__(self, *, send_as=None, send_as_error=None, **kwargs):
        super().__init__(**kwargs)
        self.send_as = send_as if send_as is not None else [ACCOUNT]
        self.send_as_error = send_as_error
        self.send_as_calls = 0

    def users(self):
        return _UsersWithSettings(self)


def _adapter(service, *, account_email=ACCOUNT):
    from channels.gmail.adapter import GmailAdapter

    return GmailAdapter(service, user_id="user-1", account_email=account_email)


# --- the measured defect ------------------------------------------------


@pytest.fixture
def self_addressed_service():
    return SendAsGmailService(
        account_email=ACCOUNT,
        sent_messages=[
            _sent("s1", ACCOUNT),
            _sent("s2", "psy.krsna@gmail.com"),
            _sent("s3", "psykrsna+news@gmail.com"),
            _sent("s4", "Ada <ada@example.com>", cc="psykrsna@gmail.com"),
        ],
    )


@pytest.mark.parametrize(
    "spelling", ["psykrsna@gmail.com", "psy.krsna@gmail.com", "psykrsna+news@gmail.com"]
)
def test_no_reply_signal_is_recorded_for_any_spelling_of_the_users_own_address(
    self_addressed_service, spelling
):
    history = _adapter(self_addressed_service).sender_history(limit=100)

    assert spelling not in history
    assert not any(
        key.replace(".", "").split("+")[0] == "psykrsna@gmail" for key in history
    )


def test_a_genuine_correspondent_in_the_same_fixture_still_produces_a_signal(
    self_addressed_service,
):
    history = _adapter(self_addressed_service).sender_history(limit=100)

    assert history["ada@example.com"].ever_replied is True
    assert history["ada@example.com"].replied_count == 1


def test_self_addresses_contribute_nothing_at_all_to_the_history(
    self_addressed_service,
):
    history = _adapter(self_addressed_service).sender_history(limit=100)

    assert list(history) == ["ada@example.com"]
    assert sum(s.replied_count for s in history.values()) == 1


# --- send-as aliases ----------------------------------------------------


def test_a_send_as_alias_is_treated_as_the_user():
    service = SendAsGmailService(
        account_email=ACCOUNT,
        send_as=[ACCOUNT, "sai@madhyamakist.com"],
        sent_messages=[
            _sent("s1", "sai@madhyamakist.com"),
            _sent("s2", "ada@example.com"),
        ],
    )

    history = _adapter(service).sender_history(limit=100)

    assert list(history) == ["ada@example.com"]


def test_alias_lookup_is_made_once_not_once_per_message():
    service = SendAsGmailService(
        account_email=ACCOUNT,
        sent_messages=[_sent(f"s{i}", "ada@example.com") for i in range(5)],
    )
    adapter = _adapter(service)

    adapter.sender_history(limit=100)
    adapter.sender_history(limit=100)

    assert service.send_as_calls == 1


def test_alias_lookup_failure_falls_back_to_the_account_and_never_fails_the_run():
    service = SendAsGmailService(
        account_email=ACCOUNT,
        send_as_error=http_error(500, "boom"),
        sent_messages=[_sent("s1", ACCOUNT), _sent("s2", "ada@example.com")],
    )

    history = _adapter(service).sender_history(limit=100)

    # Fallback: the account address alone still excludes the user.
    assert list(history) == ["ada@example.com"]


def test_an_adapter_without_a_settings_surface_still_works():
    """The plain fake has no ``users().settings()`` — best-effort must absorb it."""
    service = FakeGmailService(
        account_email=ACCOUNT,
        sent_messages=[_sent("s1", ACCOUNT), _sent("s2", "ada@example.com")],
    )

    history = _adapter(service).sender_history(limit=100)

    assert list(history) == ["ada@example.com"]


def test_account_email_is_resolved_from_the_profile_when_not_configured():
    service = SendAsGmailService(
        account_email=ACCOUNT,
        sent_messages=[_sent("s1", ACCOUNT), _sent("s2", "ada@example.com")],
    )

    history = _adapter(service, account_email="").sender_history(limit=100)

    assert list(history) == ["ada@example.com"]


# --- the no-reply flag reaches the signal -------------------------------


def test_sender_signal_carries_is_no_reply_without_a_second_parse():
    service = SendAsGmailService(
        account_email=ACCOUNT,
        sent_messages=[
            _sent("s1", "no_reply@email.apple.com"),
            _sent("s2", "ada@example.com"),
        ],
    )

    history = _adapter(service).sender_history(limit=100)

    assert history["no_reply@email.apple.com"].is_no_reply is True
    assert history["ada@example.com"].is_no_reply is False
