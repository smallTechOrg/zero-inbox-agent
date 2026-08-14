"""Integration tests for Gmail mutations + undo against the **real Gmail API**.

Mirrors ``tests/integration/test_gmail_adapter.py``'s skip-if-not-connected pattern:
if no mailbox is connected, every test SKIPS (treat as BLOCKED, not passed).

Every test in this module operates ONLY on a throwaway thread this test itself
inserts directly into the connected mailbox via ``users().messages().insert()`` —
never a real, pre-existing inbox message. The message is never sent; it is written
straight into the mailbox so this suite costs nothing and never spams anyone.
"""

from __future__ import annotations

import base64
import os
import time
import uuid
from email.mime.text import MIMEText

import pytest

SKIP_REASON = (
    "No Gmail mailbox connected — this test needs a REAL connection. "
    "Run `uv run python -m src`, open http://localhost:8001/app/ and click "
    "'Connect Gmail' (or set AGENT_TEST_GMAIL_REFRESH_TOKEN in .env). "
    "Treat this skip as BLOCKED, not as a pass."
)

TEST_LABEL_NAME = "ZeroInbox/E2ETest"


def _connection_from_production_db() -> tuple[str, str, str] | None:
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


@pytest.fixture(scope="module")
def gmail_service(gmail_connection):
    from googleapiclient.discovery import build

    from channels.gmail.oauth import credentials_from_refresh_token, google_oauth_config

    try:
        config = google_oauth_config()
    except Exception:
        pytest.skip(SKIP_REASON)
    credentials = credentials_from_refresh_token(config, gmail_connection["refresh_token"])
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


@pytest.fixture
def mutator(gmail_service):
    from channels.gmail.mutations import GmailMutator

    return GmailMutator(gmail_service, backoff_seconds=0.05)


@pytest.fixture(scope="module")
def category_label(gmail_service):
    """A real, dedicated test label — created once, reused by every test in this module."""
    from channels.gmail.labels import GmailLabelManager

    manager = GmailLabelManager(gmail_service, backoff_seconds=0.05)
    return manager.ensure_label(TEST_LABEL_NAME)


def _insert_test_thread(gmail_service, account_email: str, subject_tag: str) -> str:
    """Inserts a throwaway message directly into the mailbox (never sent) with INBOX.

    Returns the Gmail thread id. This never touches any pre-existing message.
    """
    message = MIMEText("This is a throwaway message created by an automated test.")
    message["To"] = account_email or "me"
    message["From"] = account_email or "me"
    message["Subject"] = f"[zero-inbox-agent test] {subject_tag} {uuid.uuid4().hex[:8]}"
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

    inserted = (
        gmail_service.users()
        .messages()
        .insert(userId="me", body={"raw": raw, "labelIds": ["INBOX"]})
        .execute()
    )
    return inserted["threadId"]


def _thread_labels(gmail_service, thread_id: str) -> set[str]:
    thread = gmail_service.users().threads().get(userId="me", id=thread_id, format="minimal").execute()
    labels: set[str] = set()
    for msg in thread.get("messages") or []:
        labels.update(msg.get("labelIds") or [])
    return labels


@pytest.fixture
def test_thread_id(gmail_service, gmail_connection):
    return _insert_test_thread(gmail_service, gmail_connection["account_email"], "mutations")


# --- happy path: archive_and_label then undo, verified by reading Gmail back ----


def test_archive_and_label_atomically_removes_inbox_and_adds_the_category_label(
    mutator, gmail_service, test_thread_id, category_label
):
    before = _thread_labels(gmail_service, test_thread_id)
    assert "INBOX" in before

    result = mutator.archive_and_label(test_thread_id, category_label_id=category_label["id"])

    assert result["thread_id"] == test_thread_id
    time.sleep(1)  # Gmail label propagation
    after = _thread_labels(gmail_service, test_thread_id)
    assert "INBOX" not in after
    assert category_label["id"] in after


def test_undo_archive_and_label_restores_inbox_and_removes_the_category_label(
    mutator, gmail_service, test_thread_id, category_label
):
    mutator.archive_and_label(test_thread_id, category_label_id=category_label["id"])
    time.sleep(1)
    archived = _thread_labels(gmail_service, test_thread_id)
    assert "INBOX" not in archived

    mutator.undo_archive_and_label(test_thread_id, category_label_id=category_label["id"])
    time.sleep(1)

    restored = _thread_labels(gmail_service, test_thread_id)
    assert "INBOX" in restored
    assert category_label["id"] not in restored


# --- structural guarantee: no destructive method exists anywhere ----------------


def test_gmail_mutator_has_no_trash_delete_or_spam_method():
    from channels.gmail.mutations import GmailMutator

    for forbidden in ("trash", "delete", "report_spam", "spam", "trash_thread", "delete_thread"):
        assert not hasattr(GmailMutator, forbidden), f"GmailMutator must not expose {forbidden}()"

    public_methods = {
        name
        for name in dir(GmailMutator)
        if not name.startswith("_") and callable(getattr(GmailMutator, name))
    }
    assert public_methods == {"archive_and_label", "undo_archive_and_label"}


# --- full apply/undo pipeline through tools.actions + a real DB row -------------


@pytest.fixture
def action_pipeline_row(gmail_connection, gmail_service):
    """A real Item + Category + Decision row, wired to the throwaway Gmail thread."""
    import db.session as session_module
    from db.models import Category, Decision, Item

    thread_id = _insert_test_thread(
        gmail_service, gmail_connection["account_email"], "pipeline"
    )

    with session_module._SessionLocal() as session:
        category = Category(
            user_id=gmail_connection["user_id"],
            key=f"e2e-test-{uuid.uuid4().hex[:6]}",
            name="E2ETest",
            description="throwaway test category",
            channel_label_name=TEST_LABEL_NAME,
            default_action="archive",
        )
        session.add(category)
        session.flush()

        item = Item(
            user_id=gmail_connection["user_id"],
            channel_account_id="unused-in-this-test",
            external_thread_id=thread_id,
            external_message_ids=[thread_id],
            subject="pipeline test thread",
            from_email=gmail_connection["account_email"] or "me@example.com",
            from_domain="example.com",
            snippet_redacted="",
            internal_date=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            channel_labels=["INBOX"],
        )
        session.add(item)
        session.flush()

        decision = Decision(
            user_id=gmail_connection["user_id"],
            item_id=item.id,
            run_id=_fake_run(session, gmail_connection["user_id"]),
            category_id=category.id,
            proposed_action="archive",
            confidence=0.99,
            reasoning="test",
            decided_by="rule",
            status="approved",
            # Phase 6: only a reviewed decision is appliable at all; this test
            # exercises the status guards, not the never-miss review gate.
            review_state="reviewed",
        )
        session.add(decision)
        session.commit()

        yield {
            "session_factory": session_module._SessionLocal,
            "decision_id": decision.id,
            "thread_id": thread_id,
            "user_id": gmail_connection["user_id"],
        }


def _fake_run(session, user_id: str) -> str:
    from db.models import ChannelAccount, TriageRun

    account = (
        session.query(ChannelAccount).filter(ChannelAccount.user_id == user_id).first()
    )
    run = TriageRun(
        user_id=user_id,
        channel_account_id=account.id if account else "unused",
        kind="incremental",
        status="completed",
        dry_run=False,
        items_total=1,
        items_decided=1,
    )
    session.add(run)
    session.flush()
    return run.id


def test_apply_decision_writes_action_log_before_marking_applied_and_undo_reverses_it(
    action_pipeline_row, gmail_service, gmail_connection
):
    from channels.gmail.labels import GmailLabelManager
    from channels.gmail.mutations import GmailMutator
    from db.models import Decision
    from tools.actions import apply_decision, undo_action

    session_factory = action_pipeline_row["session_factory"]
    decision_id = action_pipeline_row["decision_id"]
    user_id = action_pipeline_row["user_id"]
    thread_id = action_pipeline_row["thread_id"]

    label_manager = GmailLabelManager(gmail_service, backoff_seconds=0.05)
    mutator = GmailMutator(gmail_service, backoff_seconds=0.05)

    with session_factory() as session:
        action_log = apply_decision(
            session,
            user_id,
            decision_id,
            mutator=mutator,
            label_lookup=label_manager,
            dry_run=False,
        )
        assert action_log.undo_token is not None
        assert action_log.undo_token["thread_id"] == thread_id
        session.commit()
        action_log_id = action_log.id

    with session_factory() as session:
        decision = session.get(Decision, decision_id)
        assert decision.status == "applied"

    time.sleep(1)
    archived = _thread_labels(gmail_service, thread_id)
    assert "INBOX" not in archived

    # undo, verified by reading Gmail back
    with session_factory() as session:
        undone = undo_action(session, user_id, action_log_id, mutator=mutator)
        assert undone.undone_at is not None
        session.commit()

    time.sleep(1)
    restored = _thread_labels(gmail_service, thread_id)
    assert "INBOX" in restored

    with session_factory() as session:
        decision = session.get(Decision, decision_id)
        assert decision.status == "undone"

    # calling undo a second time is idempotent — no error, no further Gmail call
    with session_factory() as session:
        from db.models import ActionLog

        before_undone_at = session.get(ActionLog, action_log_id).undone_at
        again = undo_action(session, user_id, action_log_id, mutator=mutator)
        assert again.undone_at == before_undone_at
        session.commit()

    time.sleep(1)
    still_restored = _thread_labels(gmail_service, thread_id)
    assert "INBOX" in still_restored


# --- edge case: undo is idempotent at the tools layer without any DB row --------


def test_undo_twice_via_action_log_row_alone_is_idempotent(
    mutator, gmail_service, test_thread_id, category_label, gmail_connection
):
    """Same guarantee, driven purely by an ActionLog row (no Decision needed)."""
    import db.session as session_module
    from db.models import ActionLog

    mutator.archive_and_label(test_thread_id, category_label_id=category_label["id"])
    time.sleep(1)

    user_id = gmail_connection["user_id"]
    with session_module._SessionLocal() as session:
        action_log = ActionLog(
            user_id=user_id,
            decision_id=None,
            operation="archive",
            request_params={"thread_id": test_thread_id},
            response={},
            undo_token={
                "thread_id": test_thread_id,
                "category_label_id": category_label["id"],
                "add_label_ids": ["INBOX"],
                "remove_label_ids": [category_label["id"]],
            },
        )
        session.add(action_log)
        session.commit()
        action_log_id = action_log.id

    from tools.actions import undo_action

    with session_module._SessionLocal() as session:
        first = undo_action(session, user_id, action_log_id, mutator=mutator)
        assert first.undone_at is not None
        first_undone_at = first.undone_at
        session.commit()

    with session_module._SessionLocal() as session:
        second = undo_action(session, user_id, action_log_id, mutator=mutator)
        assert second.undone_at.replace(tzinfo=None) == first_undone_at.replace(tzinfo=None)
        session.commit()

    time.sleep(1)
    labels = _thread_labels(gmail_service, test_thread_id)
    assert "INBOX" in labels
    assert category_label["id"] not in labels


# --- error paths -----------------------------------------------------------------


def test_apply_decision_raises_dry_run_violation_and_mutates_nothing(
    action_pipeline_row, gmail_service
):
    from channels.base import DryRunViolation
    from channels.gmail.labels import GmailLabelManager
    from channels.gmail.mutations import GmailMutator
    from tools.actions import apply_decision

    session_factory = action_pipeline_row["session_factory"]
    thread_id = action_pipeline_row["thread_id"]
    before = _thread_labels(gmail_service, thread_id)

    label_manager = GmailLabelManager(gmail_service, backoff_seconds=0.05)
    mutator = GmailMutator(gmail_service, backoff_seconds=0.05)

    with session_factory() as session:
        with pytest.raises(DryRunViolation):
            apply_decision(
                session,
                action_pipeline_row["user_id"],
                action_pipeline_row["decision_id"],
                mutator=mutator,
                label_lookup=label_manager,
                dry_run=True,
            )

    after = _thread_labels(gmail_service, thread_id)
    assert after == before


def test_apply_decision_refuses_a_needs_your_call_decision_without_touching_gmail(
    gmail_connection, gmail_service
):
    import db.session as session_module
    from channels.gmail.labels import GmailLabelManager
    from channels.gmail.mutations import GmailMutator
    from db.models import Category, Decision, Item
    from tools.actions import NeedsYourCallError, apply_decision

    thread_id = _insert_test_thread(gmail_service, gmail_connection["account_email"], "needs-call")

    with session_module._SessionLocal() as session:
        category = Category(
            user_id=gmail_connection["user_id"],
            key=f"e2e-nyc-{uuid.uuid4().hex[:6]}",
            name="E2ETest",
            description="throwaway",
            channel_label_name=TEST_LABEL_NAME,
            default_action="archive",
        )
        session.add(category)
        session.flush()

        item = Item(
            user_id=gmail_connection["user_id"],
            channel_account_id="unused-in-this-test",
            external_thread_id=thread_id,
            external_message_ids=[thread_id],
            subject="needs-your-call thread",
            from_email=gmail_connection["account_email"] or "me@example.com",
            from_domain="example.com",
            snippet_redacted="",
            internal_date=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            channel_labels=["INBOX"],
        )
        session.add(item)
        session.flush()

        decision = Decision(
            user_id=gmail_connection["user_id"],
            item_id=item.id,
            run_id=_fake_run(session, gmail_connection["user_id"]),
            category_id=category.id,
            proposed_action="archive",
            confidence=0.4,
            reasoning="below floor",
            decided_by="llm",
            status="needs_your_call",
            # Phase 6: only a reviewed decision is appliable at all; this test
            # exercises the status guards, not the never-miss review gate.
            review_state="reviewed",
        )
        session.add(decision)
        session.commit()
        decision_id = decision.id

    label_manager = GmailLabelManager(gmail_service, backoff_seconds=0.05)
    mutator = GmailMutator(gmail_service, backoff_seconds=0.05)

    before = _thread_labels(gmail_service, thread_id)
    with session_module._SessionLocal() as session:
        with pytest.raises(NeedsYourCallError):
            apply_decision(
                session,
                gmail_connection["user_id"],
                decision_id,
                mutator=mutator,
                label_lookup=label_manager,
                dry_run=False,
            )
    after = _thread_labels(gmail_service, thread_id)
    assert after == before


def test_apply_decision_refuses_a_rejected_decision(gmail_connection, gmail_service):
    """Rejected decisions never reach a mutation call — enforced here as a safety net."""
    import db.session as session_module
    from channels.gmail.labels import GmailLabelManager
    from channels.gmail.mutations import GmailMutator
    from db.models import Category, Decision, Item
    from tools.actions import NotApprovedError, apply_decision

    thread_id = _insert_test_thread(gmail_service, gmail_connection["account_email"], "rejected")

    with session_module._SessionLocal() as session:
        category = Category(
            user_id=gmail_connection["user_id"],
            key=f"e2e-rej-{uuid.uuid4().hex[:6]}",
            name="E2ETest",
            description="throwaway",
            channel_label_name=TEST_LABEL_NAME,
            default_action="archive",
        )
        session.add(category)
        session.flush()

        item = Item(
            user_id=gmail_connection["user_id"],
            channel_account_id="unused-in-this-test",
            external_thread_id=thread_id,
            external_message_ids=[thread_id],
            subject="rejected thread",
            from_email=gmail_connection["account_email"] or "me@example.com",
            from_domain="example.com",
            snippet_redacted="",
            internal_date=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            channel_labels=["INBOX"],
        )
        session.add(item)
        session.flush()

        decision = Decision(
            user_id=gmail_connection["user_id"],
            item_id=item.id,
            run_id=_fake_run(session, gmail_connection["user_id"]),
            category_id=category.id,
            proposed_action="archive",
            confidence=0.9,
            reasoning="rejected by user",
            decided_by="llm",
            status="rejected",
            # Phase 6: only a reviewed decision is appliable at all; this test
            # exercises the status guards, not the never-miss review gate.
            review_state="reviewed",
        )
        session.add(decision)
        session.commit()
        decision_id = decision.id

    label_manager = GmailLabelManager(gmail_service, backoff_seconds=0.05)
    mutator = GmailMutator(gmail_service, backoff_seconds=0.05)

    before = _thread_labels(gmail_service, thread_id)
    with session_module._SessionLocal() as session:
        with pytest.raises(NotApprovedError):
            apply_decision(
                session,
                gmail_connection["user_id"],
                decision_id,
                mutator=mutator,
                label_lookup=label_manager,
                dry_run=False,
            )
    after = _thread_labels(gmail_service, thread_id)
    assert after == before
