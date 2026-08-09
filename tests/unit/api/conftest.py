"""Fixtures for the API slice: an isolated DB, a signed session, and seeded rows."""

from datetime import datetime, timedelta, timezone

import pytest

SECRET = "test-secret-key-for-sessions"


@pytest.fixture(autouse=True)
def _secret_key(monkeypatch):
    monkeypatch.setenv("AGENT_SECRET_KEY", SECRET)
    import config.settings as settings_module

    settings_module._settings = None
    yield
    settings_module._settings = None


@pytest.fixture
def db(_isolated_db):
    """A session bound to the isolated test engine (same factory the app uses)."""
    import db.session as session_module

    with session_module._SessionLocal() as session:
        yield session


@pytest.fixture
def client(_isolated_db):
    from fastapi.testclient import TestClient

    from api import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def sign_in(client):
    """Signs the test client in as a given user id by setting a real signed cookie."""

    def _sign_in(user_id: str) -> None:
        from api.session import COOKIE_NAME, issue_session_token

        client.cookies.set(COOKIE_NAME, issue_session_token(user_id))

    return _sign_in


@pytest.fixture
def seed(db):
    """Seeds two users so every route can be probed for cross-tenant leakage."""
    from db.models import (
        Category,
        ChannelAccount,
        Cluster,
        Decision,
        Item,
        Rule,
        TriageRun,
        User,
        UserSettings,
    )

    now = datetime.now(timezone.utc)
    data: dict = {}

    for tag in ("alice", "bob"):
        user = User(id=f"user-{tag}", email=f"{tag}@example.com", display_name=tag.title())
        db.add(user)
        db.add(UserSettings(user_id=user.id))
        account = ChannelAccount(
            id=f"conn-{tag}",
            user_id=user.id,
            channel="gmail",
            account_email=f"{tag}@gmail.com",
            refresh_token_enc="ENCRYPTED-DO-NOT-LEAK",
            scopes=["gmail.readonly"],
            status="connected",
            connected_at=now,
        )
        db.add(account)
        category = Category(
            id=f"cat-{tag}",
            user_id=user.id,
            key="newsletters",
            name="Newsletters",
            description="Bulk mail you subscribed to",
            channel_label_name="ZeroInbox/Newsletters",
            default_action="archive",
            is_default=True,
            sort_order=1,
        )
        db.add(category)
        rule = Rule(
            id=f"rule-{tag}",
            user_id=user.id,
            name="Substack list",
            matcher={"list_id": "substack.com"},
            action={"set_category": "newsletters", "archive": True},
            status="active",
            confidence=0.98,
        )
        db.add(rule)
        run = TriageRun(
            id=f"run-{tag}",
            user_id=user.id,
            channel_account_id=account.id,
            status="completed",
            dry_run=True,
            items_total=3,
            items_decided=3,
            counts={"by_tier": {"rule": 1, "llm": 2}, "needs_your_call": 1},
            tokens_in=120,
            tokens_out=40,
            cost_usd=0.0021,
            started_at=now - timedelta(minutes=5),
            finished_at=now - timedelta(minutes=4),
        )
        db.add(run)
        cluster = Cluster(
            id=f"cluster-{tag}",
            user_id=user.id,
            run_id=run.id,
            kind="list",
            label="Substack newsletters",
            item_count=3,
            suggested_action="archive",
            min_confidence=0.6,
            avg_confidence=0.85,
        )
        db.add(cluster)

        for idx in range(3):
            item = Item(
                id=f"item-{tag}-{idx}",
                user_id=user.id,
                channel_account_id=account.id,
                external_thread_id=f"thread-{tag}-{idx}",
                external_message_ids=[f"msg-{tag}-{idx}"],
                subject=f"{tag} subject {idx}",
                from_name="Sender Name",
                from_email="sender@substack.com",
                from_domain="substack.com",
                list_id="substack.com",
                message_count=idx + 1,
                snippet_redacted=f"redacted snippet {idx}",
                internal_date=now - timedelta(hours=idx),
                is_unread=idx == 0,
            )
            db.add(item)
            db.add(
                Decision(
                    id=f"dec-{tag}-{idx}",
                    user_id=user.id,
                    item_id=item.id,
                    run_id=run.id,
                    cluster_id=cluster.id,
                    category_id=category.id,
                    proposed_action="archive" if idx else "keep",
                    confidence=0.6 if idx == 2 else 0.93,
                    reasoning=f"reasoning for {tag} {idx}",
                    decided_by="rule" if idx == 0 else "llm",
                    rule_id=rule.id if idx == 0 else None,
                    time_sensitive=False,
                    status="needs_your_call" if idx == 2 else "proposed",
                    created_at=now,
                )
            )
        data[tag] = {"user": user, "account": account, "run": run, "cluster": cluster}

    db.commit()
    return data
