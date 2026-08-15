"""Privacy boundary: the body is unrepresentable in the classifier's type.

spec/architecture.md § Privacy Boundary: the only fields that can ever be
serialized into an LLM prompt are the privacy-allowed metadata; the prompt
builder takes a ClassifierView in which a body simply does not exist.
(The runtime tripwire — sentinel never in an outgoing payload — lives in
tests/integration/test_seam_graph_llm.py.)
"""

from __future__ import annotations

import pytest

from tests.fixtures.threads import ALLOWED_CLASSIFIER_FIELDS

FORBIDDEN_FIELDS = {"body", "body_text", "body_html", "html", "payload", "raw", "parts", "mime"}


def _classifier_view():
    import importlib

    for module in ("domain", "domain.triage", "domain.item", "domain.classifier", "graph.state"):
        try:
            mod = importlib.import_module(module)
        except ImportError:
            continue
        cls = getattr(mod, "ClassifierView", None)
        if cls is not None:
            return cls
    pytest.fail(
        "privacy contract: src/domain must define ClassifierView — the "
        "privacy-bounded dataclass that is the ONLY thing the prompt builder "
        "accepts (spec/architecture.md § Privacy Boundary)."
    )


def _field_names(cls) -> set[str]:
    import dataclasses

    if dataclasses.is_dataclass(cls):
        return {f.name for f in dataclasses.fields(cls)}
    fields = getattr(cls, "model_fields", None)  # pydantic v2
    if fields:
        return set(fields)
    annotations = getattr(cls, "__annotations__", None)
    if annotations:
        return set(annotations)
    pytest.fail("ClassifierView must be a dataclass/pydantic model with declared fields.")


class TestClassifierViewType:
    def test_no_body_shaped_field_exists(self):
        names = _field_names(_classifier_view())
        leaked = names & FORBIDDEN_FIELDS
        assert not leaked, (
            f"ClassifierView declares body-shaped field(s) {leaked} — the body "
            "must be unrepresentable in the type (spec/architecture.md)."
        )

    def test_every_field_is_on_the_privacy_allow_list(self):
        names = _field_names(_classifier_view())
        off_list = names - ALLOWED_CLASSIFIER_FIELDS
        assert not off_list, (
            f"ClassifierView declares field(s) {off_list} that are not on the "
            "privacy allow-list (sender, subject, headers, Gmail signals, "
            "snippet). Widen the spec first or drop the field."
        )

    def test_constructing_with_a_body_is_impossible(self):
        cls = _classifier_view()
        names = _field_names(cls)
        kwargs = {}
        for name in names:
            kwargs[name] = 1 if name in ("message_count", "thread_size") else "x"
        with pytest.raises((TypeError, ValueError)):
            cls(body="a leaked body", **kwargs)
