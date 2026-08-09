"""GET /api/inbox-summary — live Gmail counts, no listing, one call per label."""

from __future__ import annotations

import pytest


class FakeLabelManager:
    def __init__(self, counts: dict[str, int]):
        self._counts = counts
        self.calls: list[str] = []

    def label_count(self, label_id: str) -> int:
        self.calls.append(label_id)
        return self._counts.get(label_id, 0)


@pytest.fixture
def fake_manager(monkeypatch):
    manager = FakeLabelManager(
        {"INBOX": 87, "Label_news": 640, "Label_notif": 410}
    )
    import api.categories as categories_module

    monkeypatch.setattr(
        categories_module, "_label_manager_for_user", lambda session, user_id: manager
    )
    return manager


def test_inbox_summary_reports_live_inbox_total_and_per_category_counts(
    client, db, seed, sign_in, fake_manager
):
    from db.models import Category

    # Point user-alice's seeded category at a label the fake manager knows about.
    cat = db.get(Category, "cat-alice")
    cat.channel_label_id = "Label_news"
    db.commit()

    sign_in("user-alice")
    res = client.get("/api/inbox-summary")
    assert res.status_code == 200
    data = res.json()["data"]

    assert data["inbox_total"] == 87
    assert data["needs_your_call"] == 1  # dec-alice-2, from the seed fixture
    assert {c["key"]: c["count"] for c in data["categories"]} == {"newsletters": 640}


def test_inbox_summary_never_lists_messages_only_counts(client, seed, sign_in, fake_manager):
    """One labels().get() call per label — never a full thread listing, which
    would be the expensive/slow path this endpoint exists to avoid."""
    sign_in("user-alice")
    client.get("/api/inbox-summary")

    assert fake_manager.calls[0] == "INBOX"
    assert all(call in ("INBOX", "Label_news") for call in fake_manager.calls)


def test_inbox_summary_is_scoped_per_user(client, db, seed, sign_in, fake_manager):
    from db.models import Category

    cat = db.get(Category, "cat-bob")
    cat.channel_label_id = "Label_notif"
    db.commit()

    sign_in("user-bob")
    res = client.get("/api/inbox-summary")
    data = res.json()["data"]
    assert {c["key"]: c["count"] for c in data["categories"]} == {"newsletters": 410}


def test_inbox_summary_requires_a_session(client):
    res = client.get("/api/inbox-summary")
    assert res.status_code == 401


def test_needs_your_call_is_scoped_to_the_latest_run_not_all_time(
    client, db, seed, sign_in, fake_manager
):
    """Regression: a bare `status == needs_your_call` filter with no run scope
    accumulates without bound across every historical re-triage of the same
    inbox — it must reflect the current queue, not a running lifetime total."""
    from datetime import datetime, timedelta, timezone

    from db.models import Decision, Item, TriageRun

    now = datetime.now(timezone.utc)
    old_run = TriageRun(
        id="run-alice-stale",
        user_id="user-alice",
        channel_account_id="conn-alice",
        status="completed",
        dry_run=True,
        items_total=50,
        items_decided=50,
        counts={},
        started_at=now - timedelta(days=3),
        finished_at=now - timedelta(days=3),
    )
    db.add(old_run)
    for i in range(50):
        item = Item(
            id=f"stale-item-{i}",
            user_id="user-alice",
            channel_account_id="conn-alice",
            external_thread_id=f"stale-thread-{i}",
            external_message_ids=[f"stale-msg-{i}"],
            subject="stale",
            from_name="x",
            from_email="x@example.com",
            from_domain="example.com",
            message_count=1,
            snippet_redacted="x",
            internal_date=now - timedelta(days=3),
            is_unread=False,
        )
        db.add(item)
        db.add(
            Decision(
                id=f"stale-dec-{i}",
                user_id="user-alice",
                item_id=item.id,
                run_id=old_run.id,
                proposed_action="keep",
                confidence=0.5,
                reasoning="stale",
                decided_by="llm",
                time_sensitive=False,
                status="needs_your_call",
                created_at=now - timedelta(days=3),
            )
        )
    db.commit()

    sign_in("user-alice")
    res = client.get("/api/inbox-summary")
    # Only run-alice's own single needs_your_call decision (dec-alice-2) should
    # count — the 50 stale ones from the older run-alice-stale must not.
    assert res.json()["data"]["needs_your_call"] == 1
