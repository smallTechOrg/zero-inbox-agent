"""triage-graph slice — privacy boundary and value types (src/graph/state.py)."""

import dataclasses

import pytest

from graph.state import (
    ALLOWED_PROMPT_FIELDS,
    CONFIDENCE_REVIEW_THRESHOLD,
    ClassifierView,
    label_for,
    view_from_mapping,
)

SENTINEL = "XKCD-BODY-SENTINEL-9271"


class TestClassifierViewPrivacy:
    def test_body_is_unrepresentable_as_constructor_kwarg(self):
        with pytest.raises(TypeError):
            ClassifierView(thread_id="t1", sender_address="a@b.com",
                           body=SENTINEL)  # type: ignore[call-arg]

    def test_view_from_mapping_drops_body_and_all_disallowed_keys(self):
        view = view_from_mapping(
            {
                "thread_id": "t1",
                "subject": "Hello",
                "sender_address": "a@b.com",
                "body": SENTINEL,
                "body_html": SENTINEL,
                "raw_message": SENTINEL,
                "payload": SENTINEL,
            }
        )
        payload = view.to_prompt_dict()
        assert SENTINEL not in str(payload)
        assert set(payload) <= ALLOWED_PROMPT_FIELDS

    def test_prompt_payload_covers_exactly_the_allowed_fields(self):
        view = view_from_mapping({"thread_id": "t1"})
        assert set(view.to_prompt_dict()) == ALLOWED_PROMPT_FIELDS

    def test_view_is_immutable(self):
        view = view_from_mapping({"thread_id": "t1"})
        with pytest.raises(dataclasses.FrozenInstanceError):
            view.subject = "changed"  # type: ignore[misc]


class TestConstants:
    def test_review_threshold_is_the_spec_value(self):
        assert CONFIDENCE_REVIEW_THRESHOLD == 0.7

    def test_labels_are_namespaced(self):
        assert label_for("Finance") == "ZI/Finance"
        assert label_for("Needs review") == "ZI/Needs review"
