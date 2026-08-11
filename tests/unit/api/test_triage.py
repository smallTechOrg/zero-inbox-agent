"""The clustered triage queue: clusters and read-only thread history (autonomous mode)."""


def test_clusters_for_the_latest_run_when_no_run_id_given(client, seed, sign_in):
    sign_in("user-alice")
    res = client.get("/api/triage/clusters")
    assert res.status_code == 200
    clusters = res.json()["data"]

    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster["id"] == "cluster-alice"
    assert cluster["kind"] == "list"
    assert cluster["label"] == "Substack newsletters"
    assert cluster["item_count"] == 3
    assert cluster["suggested_action"] == "archive"
    assert cluster["min_confidence"] == 0.6
    assert cluster["avg_confidence"] == 0.85
    assert len(cluster["sample_subjects"]) == 3
    assert all(s.startswith("alice ") for s in cluster["sample_subjects"])


def test_clusters_are_scoped_to_the_signed_in_user(client, seed, sign_in):
    sign_in("user-alice")
    res = client.get("/api/triage/clusters", params={"run_id": "run-bob"})
    assert res.status_code == 200
    assert res.json()["data"] == []


def test_clusters_are_empty_before_any_run(client, db, seed, sign_in):
    from db.models import Cluster, Decision, TriageRun

    db.query(Decision).delete()
    db.query(Cluster).delete()
    db.query(TriageRun).delete()
    db.commit()

    sign_in("user-alice")
    res = client.get("/api/triage/clusters")
    assert res.status_code == 200
    assert res.json()["data"] == []


def test_items_in_a_cluster_carry_category_confidence_reasoning_and_tier(
    client, seed, sign_in
):
    sign_in("user-alice")
    res = client.get("/api/triage/items", params={"cluster_id": "cluster-alice"})
    assert res.status_code == 200
    items = res.json()["data"]
    assert len(items) == 3

    rule_row = next(i for i in items if i["decided_by"] == "rule")
    assert rule_row["category"] == "Newsletters"
    assert rule_row["confidence"] == 0.93
    assert rule_row["reasoning"] == "reasoning for alice 0"
    assert rule_row["rule_id"] == "rule-alice"
    assert rule_row["rule_name"] == "Substack list"
    assert rule_row["status"] == "proposed"
    assert rule_row["time_sensitive"] is False
    assert rule_row["item"]["subject"] == "alice subject 0"
    assert rule_row["item"]["from_email"] == "sender@substack.com"
    assert rule_row["item"]["snippet_redacted"] == "redacted snippet 0"
    assert rule_row["item"]["is_unread"] is True
    assert rule_row["item"]["internal_date"]

    llm_row = next(i for i in items if i["decided_by"] == "llm")
    assert llm_row["rule_id"] is None
    assert llm_row["rule_name"] is None


def test_items_can_be_filtered_to_the_needs_your_call_bucket(client, seed, sign_in):
    sign_in("user-alice")
    res = client.get(
        "/api/triage/items", params={"run_id": "run-alice", "status": "needs_your_call"}
    )
    assert res.status_code == 200
    items = res.json()["data"]
    assert [i["decision_id"] for i in items] == ["dec-alice-2"]
    assert items[0]["confidence"] == 0.6


def test_items_are_ordered_newest_first(client, seed, sign_in):
    sign_in("user-alice")
    items = client.get("/api/triage/items", params={"run_id": "run-alice"}).json()["data"]
    dates = [i["item"]["internal_date"] for i in items]
    assert dates == sorted(dates, reverse=True)


def test_items_of_another_users_cluster_are_not_found(client, seed, sign_in):
    sign_in("user-alice")
    res = client.get("/api/triage/items", params={"cluster_id": "cluster-bob"})
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "not_found"


def test_items_of_another_users_run_are_empty(client, seed, sign_in):
    sign_in("user-alice")
    res = client.get("/api/triage/items", params={"run_id": "run-bob"})
    assert res.status_code == 200
    assert res.json()["data"] == []


def test_items_require_a_session(client, seed):
    res = client.get("/api/triage/items", params={"run_id": "run-alice"})
    assert res.status_code == 401


def test_a_cancelled_run_never_becomes_the_default_view_over_a_completed_one(
    client, db, seed, sign_in
):
    """D11(b): a cancelled run started after a completed run must never bury
    the completed run's decisions behind the default (no run_id) view."""
    from datetime import timedelta

    from db.models import TriageRun

    completed_run = seed["alice"]["run"]
    cancelled_run = TriageRun(
        id="run-alice-cancelled",
        user_id="user-alice",
        channel_account_id=seed["alice"]["account"].id,
        status="cancelled",
        dry_run=True,
        items_total=50,
        items_decided=0,
        counts={},
        started_at=completed_run.started_at + timedelta(minutes=10),
        finished_at=completed_run.started_at + timedelta(minutes=11),
    )
    db.add(cancelled_run)
    db.commit()

    sign_in("user-alice")
    res = client.get("/api/triage/items")
    assert res.status_code == 200
    items = res.json()["data"]
    # Still the completed run's 3 decisions, none from the (empty) cancelled run.
    assert len(items) == 3
    assert all(i["decision_id"].startswith("dec-alice-") for i in items)


def test_a_running_run_can_still_be_the_default_view(client, db, seed, sign_in):
    """A user actively watching a run in progress should still see it, not the
    stale prior completed run."""
    from datetime import timedelta

    from db.models import TriageRun

    completed_run = seed["alice"]["run"]
    running_run = TriageRun(
        id="run-alice-running",
        user_id="user-alice",
        channel_account_id=seed["alice"]["account"].id,
        status="running",
        dry_run=True,
        items_total=10,
        items_decided=0,
        counts={},
        started_at=completed_run.started_at + timedelta(minutes=10),
    )
    db.add(running_run)
    db.commit()

    sign_in("user-alice")
    res = client.get("/api/triage/items")
    assert res.status_code == 200
    # The running run has no decisions yet, and it is correctly the default.
    assert res.json()["data"] == []



def test_needs_your_call_returns_only_needs_your_call_decisions(client, seed, sign_in):
    """GET /api/triage/needs-your-call returns only needs_your_call status items."""
    sign_in("user-alice")
    response = client.get("/api/triage/needs-your-call")
    assert response.status_code == 200
    data = response.json()["data"]
    assert len(data) > 0
    assert all(item["status"] == "needs_your_call" for item in data)


def test_needs_your_call_is_scoped_to_the_signed_in_user(client, seed, sign_in):
    """GET /api/triage/needs-your-call must not leak another user's items."""
    sign_in("user-alice")
    response = client.get("/api/triage/needs-your-call")
    assert response.status_code == 200
    data = response.json()["data"]
    assert all(item["decision_id"].startswith("dec-alice-") for item in data)


def test_needs_your_call_requires_a_session(client, seed):
    """Unauthenticated requests must be rejected."""
    assert client.get("/api/triage/needs-your-call").status_code == 401


def test_needs_your_call_returns_empty_before_any_run(client, db, seed, sign_in):
    """When no completed/running run exists the endpoint returns an empty list."""
    from db.models import Cluster, Decision, TriageRun

    db.query(Decision).delete()
    db.query(Cluster).delete()
    db.query(TriageRun).delete()
    db.commit()

    sign_in("user-alice")
    response = client.get("/api/triage/needs-your-call")
    assert response.status_code == 200
    assert response.json()["data"] == []


def test_categories_are_user_scoped(client, seed, sign_in):
    sign_in("user-alice")
    res = client.get("/api/categories")
    assert res.status_code == 200
    categories = res.json()["data"]
    assert [c["id"] for c in categories] == ["cat-alice"]
    assert categories[0]["name"] == "Newsletters"
    assert categories[0]["default_action"] == "archive"


def test_categories_require_a_session(client, seed):
    assert client.get("/api/categories").status_code == 401
