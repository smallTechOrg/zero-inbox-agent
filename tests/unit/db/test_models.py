"""Schema behaviour: multi-tenancy, constraints, and the audit trail shape."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from db.models import (
    ALLOWED_ACTION_OPERATIONS,
    ActionLog,
    Base,
    Category,
    ChannelAccount,
    Cluster,
    Correction,
    Decision,
    Item,
    LLMCall,
    Rule,
    SenderProfile,
    TriageRun,
    User,
    UserSettings,
)


def _now():
    return datetime.now(timezone.utc)


@pytest.fixture
def session(_isolated_db) -> Session:
    factory = sessionmaker(bind=_isolated_db, autoflush=False, autocommit=False)
    with factory() as s:
        yield s


def _make_user(s: Session, email: str) -> User:
    user = User(email=email, display_name=email.split("@")[0])
    s.add(user)
    s.flush()
    s.add(UserSettings(user_id=user.id))
    s.flush()
    return user


def _make_account(s: Session, user: User, addr: str | None = None) -> ChannelAccount:
    account = ChannelAccount(
        user_id=user.id,
        channel="gmail",
        account_email=addr or user.email,
        refresh_token_enc="gAAAAA-ciphertext",
        scopes=["gmail.readonly"],
    )
    s.add(account)
    s.flush()
    return account


def _make_item(s: Session, user: User, account: ChannelAccount, thread_id: str) -> Item:
    item = Item(
        user_id=user.id,
        channel_account_id=account.id,
        external_thread_id=thread_id,
        external_message_ids=[f"{thread_id}-m1"],
        subject="Weekly digest",
        from_name="Substack",
        from_email="post@substack.com",
        from_domain="substack.com",
        to_emails=[user.email],
        list_id="<weekly.substack.com>",
        message_count=1,
        snippet_redacted="A redacted preview",
        internal_date=_now(),
        is_unread=True,
        channel_labels=["INBOX"],
    )
    s.add(item)
    s.flush()
    return item


def _make_run(s: Session, user: User, account: ChannelAccount) -> TriageRun:
    run = TriageRun(
        user_id=user.id,
        channel_account_id=account.id,
        kind="incremental",
        status="running",
        dry_run=True,
        items_total=1,
    )
    s.add(run)
    s.flush()
    return run


# --- happy path ---------------------------------------------------------------------


def test_full_triage_row_graph_persists_with_tier_and_reasoning(session):
    user = _make_user(session, "founder@example.com")
    account = _make_account(session, user)
    item = _make_item(session, user, account, "thread-1")
    run = _make_run(session, user, account)

    category = Category(
        user_id=user.id,
        key="newsletters",
        name="Newsletters",
        description="Bulk subscriptions the user opted into.",
        channel_label_name="ZeroInbox/Newsletters",
        default_action="archive",
        is_default=True,
    )
    cluster = Cluster(
        user_id=user.id,
        run_id=run.id,
        kind="list",
        label="Substack newsletters",
        item_count=1,
        suggested_action="archive",
        min_confidence=0.9,
        avg_confidence=0.9,
    )
    session.add_all([category, cluster])
    session.flush()

    decision = Decision(
        user_id=user.id,
        item_id=item.id,
        run_id=run.id,
        cluster_id=cluster.id,
        category_id=category.id,
        proposed_action="archive",
        confidence=0.91,
        reasoning="List-Id header present and sender has never been replied to.",
        decided_by="rule",
        status="proposed",
    )
    session.add(decision)
    session.commit()

    loaded = session.scalars(select(Decision).where(Decision.user_id == user.id)).one()
    assert loaded.decided_by == "rule"
    assert loaded.confidence == pytest.approx(0.91)
    assert loaded.reasoning.startswith("List-Id header")
    assert loaded.cluster_id == cluster.id
    assert session.get(Item, item.id).snippet_redacted == "A redacted preview"
    assert session.get(TriageRun, run.id).dry_run is True


def test_user_settings_defaults_match_spec(session):
    user = _make_user(session, "defaults@example.com")
    session.commit()
    settings = session.get(UserSettings, user.id)
    # Phase 7: 0.95 -> 0.80. The old default sat above the model's entire
    # measured output range (~0.94 ceiling), so it was a bar nothing could clear.
    # See spec/data.md § user_settings.
    assert settings.auto_act_threshold == pytest.approx(0.80)
    assert settings.confidence_floor == pytest.approx(0.75)
    assert settings.dry_run is True
    assert settings.llm_model == "nvidia/nemotron-3-nano-30b-a3b"
    assert settings.digest_hour_local == 8
    assert settings.timezone == "UTC"


def test_every_user_scoped_table_carries_user_id():
    from db.models import NON_USER_SCOPED_TABLES

    for table in Base.metadata.sorted_tables:
        if table.name in NON_USER_SCOPED_TABLES:
            continue
        names = {c.name for c in table.columns}
        if table.name == "users":
            # `users.id` IS the tenant key.
            assert "id" in names
            continue
        assert "user_id" in names, f"{table.name} is not user-scoped"
        column = table.c.user_id
        isolatable = (
            column.primary_key
            or column.index
            or any("user_id" in {c.name for c in idx.columns} for idx in table.indexes)
        )
        assert isolatable, f"{table.name}.user_id is not efficiently isolatable"


def test_queries_are_isolated_between_tenants(session):
    a = _make_user(session, "a@example.com")
    b = _make_user(session, "b@example.com")
    acc_a, acc_b = _make_account(session, a), _make_account(session, b)
    _make_item(session, a, acc_a, "shared-thread-id")
    _make_item(session, b, acc_b, "shared-thread-id")
    session.commit()

    a_items = session.scalars(select(Item).where(Item.user_id == a.id)).all()
    b_items = session.scalars(select(Item).where(Item.user_id == b.id)).all()
    assert len(a_items) == 1 and len(b_items) == 1
    assert a_items[0].id != b_items[0].id


# --- edge cases ---------------------------------------------------------------------


def test_same_thread_id_for_two_users_is_allowed_but_duplicate_per_user_is_not(session):
    user = _make_user(session, "dupe@example.com")
    account = _make_account(session, user)
    _make_item(session, user, account, "t-dup")
    session.commit()

    with pytest.raises(IntegrityError):
        _make_item(session, user, account, "t-dup")
    session.rollback()


def test_item_tolerates_empty_headers_and_no_list_id(session):
    user = _make_user(session, "empty@example.com")
    account = _make_account(session, user)
    item = Item(
        user_id=user.id,
        channel_account_id=account.id,
        external_thread_id="bare",
    )
    session.add(item)
    session.commit()
    loaded = session.get(Item, item.id)
    assert loaded.subject == ""
    assert loaded.list_id is None
    assert loaded.snippet_redacted == ""
    assert loaded.message_count == 1
    assert loaded.has_attachments is False


def test_decision_is_idempotent_per_run_and_item(session):
    """The uniqueness that makes a cancelled run resumable without redoing work."""
    user = _make_user(session, "resume@example.com")
    account = _make_account(session, user)
    item = _make_item(session, user, account, "t-resume")
    run = _make_run(session, user, account)
    session.add(
        Decision(
            user_id=user.id, item_id=item.id, run_id=run.id, decided_by="llm", confidence=0.5
        )
    )
    session.commit()

    session.add(
        Decision(
            user_id=user.id, item_id=item.id, run_id=run.id, decided_by="llm", confidence=0.6
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_sender_profile_records_reply_history_signal(session):
    user = _make_user(session, "reply@example.com")
    profile = SenderProfile(
        user_id=user.id,
        sender_email="cofounder@startup.com",
        sender_domain="startup.com",
        received_count=12,
        replied_count=3,
        ever_replied=True,
        last_replied_at=_now() - timedelta(days=2),
        importance_score=0.9,
    )
    session.add(profile)
    session.commit()
    loaded = session.scalars(
        select(SenderProfile).where(
            SenderProfile.user_id == user.id, SenderProfile.ever_replied.is_(True)
        )
    ).one()
    assert loaded.sender_email == "cofounder@startup.com"


def test_duplicate_connection_for_same_mailbox_is_rejected(session):
    user = _make_user(session, "conn@example.com")
    _make_account(session, user, "inbox@gmail.com")
    session.commit()
    with pytest.raises(IntegrityError):
        _make_account(session, user, "inbox@gmail.com")
    session.rollback()


def test_duplicate_category_key_per_user_is_rejected(session):
    user = _make_user(session, "cat@example.com")
    for _ in range(2):
        session.add(
            Category(user_id=user.id, key="receipts", name="Receipts", sort_order=1)
        )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


# --- error paths --------------------------------------------------------------------


def test_decision_requires_a_deciding_tier(session):
    user = _make_user(session, "tier@example.com")
    account = _make_account(session, user)
    item = _make_item(session, user, account, "t-tier")
    run = _make_run(session, user, account)
    session.add(Decision(user_id=user.id, item_id=item.id, run_id=run.id))  # no decided_by
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_channel_account_requires_an_encrypted_refresh_token(session):
    user = _make_user(session, "token@example.com")
    session.add(
        ChannelAccount(user_id=user.id, channel="gmail", account_email="x@gmail.com")
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_user_email_is_unique(session):
    _make_user(session, "same@example.com")
    session.commit()
    session.add(User(email="same@example.com"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_no_destructive_operation_is_an_allowed_action(session):
    assert ALLOWED_ACTION_OPERATIONS == {
        "archive",
        "add_label",
        "remove_label",
        "create_filter",
        "create_draft",
    }
    for forbidden in ("delete", "trash", "spam", "send"):
        assert forbidden not in ALLOWED_ACTION_OPERATIONS


def test_action_log_and_correction_carry_undo_and_training_signal(session):
    user = _make_user(session, "audit@example.com")
    account = _make_account(session, user)
    item = _make_item(session, user, account, "t-audit")
    run = _make_run(session, user, account)
    decision = Decision(
        user_id=user.id,
        item_id=item.id,
        run_id=run.id,
        decided_by="llm",
        proposed_action="archive",
        confidence=0.8,
        status="applied",
    )
    session.add(decision)
    session.flush()

    session.add(
        ActionLog(
            user_id=user.id,
            decision_id=decision.id,
            operation="archive",
            request_params={"thread_id": "t-audit", "removeLabelIds": ["INBOX"]},
            response={"id": "t-audit"},
            undo_token={"operation": "add_label", "addLabelIds": ["INBOX"]},
        )
    )
    session.add(
        Correction(
            user_id=user.id,
            item_id=item.id,
            decision_id=decision.id,
            from_action="archive",
            to_action="keep",
            source="observed_unarchive",
        )
    )
    session.commit()

    log = session.scalars(select(ActionLog).where(ActionLog.user_id == user.id)).one()
    assert log.undo_token["operation"] == "add_label"
    assert log.undone_at is None
    correction = session.scalars(
        select(Correction).where(Correction.user_id == user.id)
    ).one()
    assert (correction.from_action, correction.to_action) == ("archive", "keep")


def test_llm_call_cost_accounting_is_scoped_to_run_and_user(session):
    user = _make_user(session, "cost@example.com")
    account = _make_account(session, user)
    run = _make_run(session, user, account)
    session.add(
        LLMCall(
            user_id=user.id,
            run_id=run.id,
            purpose="classify",
            model="nvidia/nemotron-3-nano-30b-a3b",
            items_in_batch=25,
            tokens_in=4200,
            tokens_out=800,
            cost_usd=0.0,
            latency_ms=1300,
        )
    )
    session.commit()
    call = session.scalars(select(LLMCall).where(LLMCall.run_id == run.id)).one()
    assert call.items_in_batch == 25
    assert call.model == "nvidia/nemotron-3-nano-30b-a3b"


def test_rule_defaults_to_proposed_and_stores_matcher_json(session):
    user = _make_user(session, "rule@example.com")
    rule = Rule(
        user_id=user.id,
        name="Archive Substack",
        kind="deterministic",
        source="mined",
        matcher={"list_id": "<weekly.substack.com>"},
        action={"set_category": "newsletters", "archive": True, "digest": False},
        confidence=0.88,
    )
    session.add(rule)
    session.commit()
    loaded = session.scalars(
        select(Rule).where(Rule.user_id == user.id, Rule.status == "proposed")
    ).one()
    assert loaded.matcher["list_id"] == "<weekly.substack.com>"
    assert loaded.action["archive"] is True
    assert loaded.match_count == 0
    assert loaded.channel_filter_id is None
