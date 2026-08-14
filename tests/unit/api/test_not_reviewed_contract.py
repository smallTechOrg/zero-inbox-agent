"""A provisional decision must fail apply with 422 not_reviewed — never 404.

Regression: NotReviewedError subclasses ActionsError and had no dedicated
handler, so it fell through the generic `except ActionsError -> 404 not_found`.
The API told the client the decision did not exist, when in fact it exists and
is merely still awaiting the never-miss reviewer. spec/api.md:133 requires
`422 not_reviewed`.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


@pytest.fixture
def provisional_decision(db, seed):
    """A live-but-unreviewed decision, with dry_run off so apply is reachable."""
    from db.models import Decision, UserSettings

    settings = db.get(UserSettings, "user-alice")
    settings.dry_run = False

    decision = db.get(Decision, "dec-alice-1")
    decision.status = "approved"
    decision.proposed_action = "archive"
    decision.review_state = "provisional"
    db.commit()
    return decision.id


def test_applying_a_provisional_decision_returns_422_not_reviewed(
    client, seed, sign_in, provisional_decision, monkeypatch
):
    import api.actions as actions

    # Never build a real Gmail client — the guard must fire before any mutator.
    class _Boom:
        def archive_and_label(self, *a, **k):
            raise AssertionError("a provisional decision must never reach Gmail")

    class _Labels:
        def ensure_label(self, name):
            return {"id": "L1", "name": name}

    monkeypatch.setattr(
        actions, "_mutator_and_labels_for_user", lambda s, u: (_Boom(), _Labels())
    )

    sign_in("user-alice")
    res = client.post(
        "/api/actions/apply", json={"decision_ids": [provisional_decision]}
    )

    assert res.status_code == 422, res.json()
    assert res.json()["error"]["code"] == "not_reviewed"


def test_force_true_does_not_bypass_the_review_gate_over_the_api(
    client, seed, sign_in, provisional_decision, monkeypatch
):
    """force=True exists to override the keep-proposed guard only. It must never
    let un-reviewed work reach the mailbox — that is the never-miss guarantee."""
    import api.actions as actions

    class _Boom:
        def archive_and_label(self, *a, **k):
            raise AssertionError("force must not bypass the review gate")

    class _Labels:
        def ensure_label(self, name):
            return {"id": "L1", "name": name}

    monkeypatch.setattr(
        actions, "_mutator_and_labels_for_user", lambda s, u: (_Boom(), _Labels())
    )

    sign_in("user-alice")
    res = client.post(
        "/api/actions/apply",
        json={"decision_ids": [provisional_decision], "force": True},
    )

    assert res.status_code == 422, res.json()
    assert res.json()["error"]["code"] == "not_reviewed"
