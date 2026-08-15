"""Seam: taxonomy API ↔ db. CRUD, seed defaults, and the reserved category.

(The taxonomy → graph seam — "the agent classifies against the CURRENT
taxonomy" — is proven with a real LLM in test_seam_graph_llm.py.)
"""

from __future__ import annotations

import pytest

from tests.conftest import DEFAULT_CATEGORY_SEED
from tests.integration._helpers import envelope_error, envelope_ok

pytestmark = pytest.mark.integration


def _categories(client) -> list[dict]:
    data = envelope_ok(client.get("/api/taxonomy"))
    return data if isinstance(data, list) else data.get("categories", [])


def _by_name(cats: list[dict], name: str) -> dict:
    match = [c for c in cats if c.get("name") == name]
    assert match, f"category {name!r} not in {[c.get('name') for c in cats]}"
    return match[0]


class TestDefaults:
    def test_seeded_defaults_are_returned_in_order(self, auth_client):
        cats = _categories(auth_client)
        names = [c["name"] for c in cats]
        expected = [name for name, _, _ in DEFAULT_CATEGORY_SEED]
        assert names == expected, f"taxonomy order/content drifted: {names}"

    def test_needs_review_is_flagged_reserved(self, auth_client):
        nr = _by_name(_categories(auth_client), "Needs review")
        assert nr.get("is_needs_review") is True
        assert nr.get("rule") == "label_only"


class TestCrud:
    def test_add_edit_delete_roundtrip(self, auth_client):
        created = envelope_ok(
            auth_client.post(
                "/api/taxonomy",
                json={"name": "Receipts", "description": "order receipts", "rule": "label_only"},
            )
        )
        cat_id = created.get("id") or _by_name(_categories(auth_client), "Receipts")["id"]

        envelope_ok(
            auth_client.patch(f"/api/taxonomy/{cat_id}", json={"name": "Order receipts"})
        )
        assert _by_name(_categories(auth_client), "Order receipts")["id"] == cat_id

        envelope_ok(
            auth_client.patch(f"/api/taxonomy/{cat_id}", json={"rule": "label_and_archive"})
        )
        assert _by_name(_categories(auth_client), "Order receipts")["rule"] == "label_and_archive"

        envelope_ok(auth_client.delete(f"/api/taxonomy/{cat_id}"))
        assert "Order receipts" not in [c["name"] for c in _categories(auth_client)]

    def test_invalid_rule_is_rejected(self, auth_client):
        response = auth_client.post(
            "/api/taxonomy",
            json={"name": "Broken", "description": "", "rule": "delete_everything"},
        )
        assert 400 <= response.status_code < 500
        envelope_error(response)

    def test_empty_name_is_rejected(self, auth_client):
        response = auth_client.post(
            "/api/taxonomy", json={"name": "", "description": "", "rule": "label_only"}
        )
        assert 400 <= response.status_code < 500


class TestGuards:
    def test_deleting_needs_review_is_409(self, auth_client):
        nr = _by_name(_categories(auth_client), "Needs review")
        response = auth_client.delete(f"/api/taxonomy/{nr['id']}")
        assert response.status_code == 409
        envelope_error(response)

    def test_deleting_an_in_use_category_is_409_with_a_clear_message(
        self, auth_client, db_session, seeded_user
    ):
        from db import models

        user, categories = seeded_user
        finance = categories["Finance"]
        run = models.Run(
            id="test-run-inuse", user_id=user.id, status="completed",
            trigger="clean_chunk", chunk_limit=50,
        )
        db_session.add(run)
        db_session.add(
            models.ThreadDecision(
                id="test-dec-inuse",
                user_id=user.id,
                run_id="test-run-inuse",
                gmail_thread_id="test-thr-inuse",
                sender="acme@example.com",
                subject="invoice",
                snippet="…",
                category_id=finance.id,
                confidence=0.95,
                reason="obvious invoice",
                needs_review=False,
                source="llm",
                undone=False,
            )
        )
        db_session.commit()

        response = auth_client.delete(f"/api/taxonomy/{finance.id}")
        assert response.status_code == 409
        error = envelope_error(response)
        assert error["message"], "the 409 must explain itself in plain English"
