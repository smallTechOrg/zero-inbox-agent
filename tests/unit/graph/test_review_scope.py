"""Phase 9, item zero: the reviewer's scope is *mutability*, not the word "archive".

`spec/roadmap.md` § Phase 9 "Item zero", `spec/capabilities/never-miss-safeguards.md`.

`digest` reaches the same `archive_and_label(remove_label_ids=[INBOX])` mutation as
`archive` (`tools.actions.apply_decision` permits both), but the pre-Phase-9
reviewer audited `proposed_action == "archive"` only. 171 live `digest` decisions
were therefore never audited, and 44 were applied.

These tests make the two sets ONE contract. If a future third mutating action is
added to `tools.actions.MUTABLE_ACTIONS` and not to
`graph.nodes_review.REVIEWABLE_ACTIONS` (or the reverse), the first test here goes
red — the hole cannot be silently reopened.
"""

from __future__ import annotations

import pytest


def _decision(item_id: str, action: str, **extra) -> dict:
    base = {
        "item_id": item_id,
        "category": "newsletters",
        "proposed_action": action,
        "confidence": 0.9,
        "reasoning": "bulk",
        "decided_by": "llm",
        "status": "proposed",
    }
    base.update(extra)
    return base


def _item(item_id: str) -> dict:
    return {
        "id": item_id,
        "from_name": "Sender",
        "from_email": "s@example.com",
        "from_domain": "example.com",
        "subject": f"subject {item_id}",
        "snippet_redacted": "snippet",
    }


# --------------------------------------------------------------------------
# The contract
# --------------------------------------------------------------------------


def test_reviewable_actions_equals_mutable_actions():
    """The audited set and the mutable set are the same set. No gap, ever."""
    from graph.nodes_review import REVIEWABLE_ACTIONS
    from tools import actions

    assert hasattr(actions, "MUTABLE_ACTIONS"), (
        "tools.actions.MUTABLE_ACTIONS is missing. It is the pinned Phase 9 "
        "cross-slice contract (slice 3 writes it, slice 1 asserts against it): "
        "the set of proposed_actions apply_decision will mutate."
    )
    assert REVIEWABLE_ACTIONS == actions.MUTABLE_ACTIONS, (
        "graph.nodes_review.REVIEWABLE_ACTIONS != tools.actions.MUTABLE_ACTIONS. "
        "Any action the mutator will perform MUST be audited by the never-miss "
        "reviewer; a mutable-but-unreviewable action is Phase 9 item zero all "
        "over again."
    )


def test_digest_is_reviewable_and_archive_is_too():
    from graph.nodes_review import REVIEWABLE_ACTIONS

    assert "archive" in REVIEWABLE_ACTIONS
    assert "digest" in REVIEWABLE_ACTIONS, (
        "digest reaches archive_and_label(remove_label_ids=[INBOX]) — it is a "
        "mutation and must be reviewed."
    )
    assert "keep" not in REVIEWABLE_ACTIONS
    assert "reply" not in REVIEWABLE_ACTIONS


def test_apply_decision_permits_exactly_the_reviewable_actions():
    """Belt and braces: the *source* of apply_decision's action gate agrees.

    `MUTABLE_ACTIONS` being equal is only meaningful if `apply_decision` actually
    consults it. This reads the guard's own membership test.
    """
    import inspect

    from graph.nodes_review import REVIEWABLE_ACTIONS
    from tools import actions

    source = inspect.getsource(actions.apply_decision)
    assert "MUTABLE_ACTIONS" in source, (
        "apply_decision does not consult MUTABLE_ACTIONS — the constant is "
        "plumbed but not wired, so the equality assertion above proves nothing."
    )
    assert REVIEWABLE_ACTIONS == actions.MUTABLE_ACTIONS


# --------------------------------------------------------------------------
# The reviewer's batch actually contains the digest proposals
# --------------------------------------------------------------------------


class _RecordingReview:
    """Stands in for the LLM reviewer; records every batch it was handed."""

    def __init__(self) -> None:
        self.batches: list[list[dict]] = []

    def __call__(self, decisions, items_by_id, **kwargs):
        self.batches.append(list(decisions))
        return {}, [], False

    @property
    def seen_item_ids(self) -> set[str]:
        return {d["item_id"] for batch in self.batches for d in batch}


@pytest.fixture
def recorded(monkeypatch):
    from graph import nodes_review

    spy = _RecordingReview()
    monkeypatch.setattr(nodes_review, "review_archive_batch", spy)
    return spy


def test_reviewer_batch_includes_digest_proposals(recorded, monkeypatch):
    from graph import nodes_review

    state = {
        "run_id": "run-scope-1",
        "user_id": "test-user-scope",
        "decisions": [
            _decision("it-archive", "archive"),
            _decision("it-digest", "digest"),
            _decision("it-keep", "keep"),
        ],
        "items": [_item("it-archive"), _item("it-digest"), _item("it-keep")],
        "settings": {},
    }

    out = nodes_review.second_pass_reviewer(state)

    assert recorded.seen_item_ids == {"it-archive", "it-digest"}, (
        "the reviewer must audit every mutating proposal; a digest proposal that "
        "never reaches a batch is an unreviewed mutation"
    )
    assert set(out["audited_item_ids"]) == {"it-archive", "it-digest"}
    assert "it-keep" not in out["audited_item_ids"]


def test_reviewer_skips_needs_your_call_rows(recorded):
    from graph import nodes_review

    state = {
        "run_id": "run-scope-2",
        "user_id": "test-user-scope",
        "decisions": [
            _decision("it-a", "digest"),
            _decision("it-b", "digest", status="needs_your_call"),
        ],
        "items": [_item("it-a"), _item("it-b")],
        "settings": {},
    }
    out = nodes_review.second_pass_reviewer(state)
    assert recorded.seen_item_ids == {"it-a"}
    assert out["audited_item_ids"] == ["it-a"]


def test_failed_batch_is_never_reported_as_audited(monkeypatch):
    from graph import nodes_review

    def _always_fails(decisions, items_by_id, **kwargs):
        return {}, [], True

    monkeypatch.setattr(nodes_review, "review_archive_batch", _always_fails)
    state = {
        "run_id": "run-scope-3",
        "user_id": "test-user-scope",
        "decisions": [_decision("it-a", "archive"), _decision("it-b", "digest")],
        "items": [_item("it-a"), _item("it-b")],
        "settings": {},
    }
    out = nodes_review.second_pass_reviewer(state)
    assert out["audited_item_ids"] == []
    assert out["review_failed_item_ids"] == ["it-a", "it-b"]
    assert nodes_review.audited_item_ids_for("run-scope-3") == []


def test_audited_set_is_published_for_the_downstream_node(recorded):
    """The hand-off is a registry, not a state key LangGraph would drop.

    `audited_item_ids` is not a declared TriageState channel, so a return-value-only
    hand-off would be silently discarded between nodes. This asserts the transport
    that actually carries it.
    """
    from graph import nodes_review

    state = {
        "run_id": "run-scope-4",
        "user_id": "test-user-scope",
        "decisions": [_decision("it-a", "archive"), _decision("it-b", "digest")],
        "items": [_item("it-a"), _item("it-b")],
        "settings": {},
    }
    nodes_review.second_pass_reviewer(state)
    assert nodes_review.audited_item_ids_for("run-scope-4") == ["it-a", "it-b"]

    nodes_review.forget_audited("run-scope-4")
    assert nodes_review.audited_item_ids_for("run-scope-4") == []


def test_unknown_run_fails_closed():
    """No published audited set means NOTHING is treated as audited."""
    from graph import nodes_review

    assert nodes_review.audited_item_ids_for("run-that-never-ran") == []
    assert nodes_review.audited_item_ids_for(None) == []
