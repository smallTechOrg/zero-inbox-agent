"""The per-category autonomy bar, tested THROUGH the seam that carries it.

Both ends of this contract were already covered independently: ``db.seed`` writes
Receipts at ``auto_act_threshold = 0.85`` (tests/unit/tools/test_taxonomy_autonomy.py)
and ``graph.autonomy`` honours a category override (tests/unit/graph/test_autonomy.py).
Neither end tested the *wire* between them — ``graph.persistence.load_context``, which
copies the column into ``state["categories"]`` and is the only thing that makes the
0.85 bar reach the policy.

Delete ``"auto_act_threshold"`` from the dict in ``graph/persistence.py`` and neither
end-test notices: Receipts and Outreach silently degrade from their 0.85 bar to the
0.80 global and the agent starts archiving mail it was configured not to touch — no
crash, no red test. These tests go through ``load_context`` rather than around it, so
that deletion turns them red. Verified by doing exactly that.
"""

from __future__ import annotations

import pytest

from db.seed import ensure_default_taxonomy
from graph.nodes import _load_context_rows
from graph.nodes_autonomy import align_to_category_default, mark_autonomy_state
from tools.taxonomy import list_categories

USER_ID = "user-threshold-seam"

#: Between the 0.80 global bar and the 0.85 Receipts bar. The whole point.
UNDER_CATEGORY_BAR = 0.82
OVER_CATEGORY_BAR = 0.86


def _session():
    import db.session as session_module

    return session_module._SessionLocal()


def _seeded_categories() -> list[dict]:
    """Seed a real Receipts row at 0.85 and read it back through the real seam."""
    with _session() as session:
        ensure_default_taxonomy(session, USER_ID)
        session.commit()
        receipts = next(c for c in list_categories(session, USER_ID) if c.key == "receipts")
        assert receipts.auto_act_threshold == pytest.approx(0.85)
        assert receipts.default_action == "archive"
        context = _load_context_rows(session, USER_ID)
    return context["categories"]


def _state(categories: list[dict], confidence: float, action: str = "archive") -> dict:
    return {
        "run_id": "run-seam",
        "user_id": USER_ID,
        "categories": categories,
        "settings": {},  # global bar = 0.80, confidence floor = 0.75
        "items": [{"id": "item-1", "from_email": "billing@shop.example", "subject": "Receipt"}],
        "decisions": [
            {
                "item_id": "item-1",
                "category": "receipts",
                "proposed_action": action,
                "confidence": confidence,
                "status": "proposed",
                "decided_by": "llm",
            }
        ],
    }


class TestTheCategoryBarSurvivesTheSeam:
    def test_load_context_carries_the_per_category_threshold(self):
        by_key = {c["key"]: c for c in _seeded_categories()}
        assert by_key["receipts"]["auto_act_threshold"] == pytest.approx(0.85)
        # An unconfigured category must inherit the global bar, not fabricate one.
        assert by_key["newsletters"]["auto_act_threshold"] is None

    def test_a_receipt_over_the_category_bar_ends_auto_act(self):
        categories = _seeded_categories()
        result = mark_autonomy_state(_state(categories, OVER_CATEGORY_BAR))
        assert result["error"] is None
        assert result["decisions"][0]["autonomy_state"] == "auto_act"

    def test_a_receipt_under_the_category_bar_ends_below_threshold(self):
        """0.82 clears the 0.80 global bar and MUST still be held by the 0.85
        Receipts bar. This is the assertion that dies if the seam drops the key."""
        categories = _seeded_categories()
        result = mark_autonomy_state(_state(categories, UNDER_CATEGORY_BAR))
        assert result["error"] is None
        assert result["decisions"][0]["autonomy_state"] == "below_threshold"

    def test_alignment_also_respects_the_category_bar_through_the_seam(self):
        """The other consumer of the same seam: a `keep` at 0.82 is not converted
        to `archive`, but the same `keep` at 0.86 is."""
        categories = _seeded_categories()

        held = align_to_category_default(_state(categories, UNDER_CATEGORY_BAR, action="keep"))
        assert held["error"] is None
        assert held["decisions"][0]["proposed_action"] == "keep"

        converted = align_to_category_default(
            _state(categories, OVER_CATEGORY_BAR, action="keep")
        )
        assert converted["error"] is None
        assert converted["decisions"][0]["proposed_action"] == "archive"

    def test_an_unconfigured_category_still_uses_the_global_bar(self):
        """Guards the inverse mistake: the seam must not stamp every category with
        the Receipts bar. Newsletters (threshold None) acts at 0.82."""
        categories = _seeded_categories()
        state = _state(categories, UNDER_CATEGORY_BAR)
        state["decisions"][0]["category"] = "newsletters"
        result = mark_autonomy_state(state)
        assert result["decisions"][0]["autonomy_state"] == "auto_act"
