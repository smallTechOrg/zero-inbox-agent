"""Taxonomy CRUD contract — spec/api.md § Audit & Taxonomy (Phase 1).

Covers, per spec/capabilities/taxonomy-management.md:

* happy path — lazy seed on first GET, add / rename / rule-edit / delete, with
  DB state asserted after every mutation;
* edge cases — empty/oversized name, bad rule, duplicate name (case-insensitive);
* error paths — delete-in-use 409, "Needs review" reserved (delete + rule 409),
  ``?merge_into`` refused in Phase 1, cross-user isolation (404, not 403),
  unauthenticated 401 ``signed_out``.

The router is mounted on a throwaway app (``src/api/app.py`` is owned by
another slice); auth is exercised through the slice's own dependency seam,
overridden per-test exactly as FastAPI intends. The signed-cookie path itself
belongs to the auth-gmail slice's tests.
"""

from __future__ import annotations

import pytest

ALICE = "test-user-alice"
BOB = "test-user-bob"

SEED_NAMES = [
    "Finance",
    "Newsletters",
    "Notifications",
    "Personal",
    "Shopping",
    "Travel",
    "Needs review",
]


@pytest.fixture
def taxonomy_app(_isolated_db):
    """The router on a bare app.

    ``api.app`` builds the WHOLE application at import time and other Phase-1
    slices are landing in parallel, so the envelope handlers are replicated
    here from the canonical ``api._common.error_body`` — same wire shape,
    no cross-slice import. The real wiring is asserted in TestWiringSeam.
    """
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import JSONResponse

    from api import taxonomy
    from api._common import error_body

    app = FastAPI()

    @app.exception_handler(HTTPException)
    async def _http_exc(request, exc):
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            return JSONResponse(
                status_code=exc.status_code,
                content=error_body(detail["code"], detail.get("message", "")),
            )
        return JSONResponse(
            status_code=exc.status_code, content=error_body("http_error", str(detail))
        )

    app.include_router(taxonomy.router)
    return app


@pytest.fixture
def client(taxonomy_app):
    from fastapi.testclient import TestClient

    with TestClient(taxonomy_app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def sign_in(taxonomy_app):
    """Authenticate the client as a user via the slice's auth dependency seam."""

    def _sign_in(user_id: str) -> None:
        from api import taxonomy

        taxonomy_app.dependency_overrides[taxonomy.current_user_id] = lambda: user_id

    yield _sign_in
    taxonomy_app.dependency_overrides.clear()


def _db():
    import db.session as session_module

    return session_module._SessionLocal()


def _categories(user_id):
    from sqlalchemy import select

    from db.models import Category

    with _db() as session:
        return list(
            session.execute(
                select(Category)
                .where(Category.user_id == user_id)
                .order_by(Category.position)
            ).scalars()
        )


# --- happy path --------------------------------------------------------------


class TestSeedAndList:
    def test_first_get_seeds_the_defaults_ordered_with_rules(self, client, sign_in):
        sign_in(ALICE)
        res = client.get("/api/taxonomy")
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        cats = body["data"]["categories"]
        assert [c["name"] for c in cats] == SEED_NAMES
        by_name = {c["name"]: c for c in cats}
        assert by_name["Newsletters"]["rule"] == "label_and_archive"
        assert by_name["Finance"]["rule"] == "label_only"
        assert by_name["Needs review"]["is_needs_review"] is True
        assert sum(c["is_needs_review"] for c in cats) == 1
        # gmail labels are created lazily — none yet.
        assert all(c["gmail_label_id"] is None for c in cats)
        # DB state: the rows really exist.
        assert len(_categories(ALICE)) == 7

    def test_get_is_idempotent(self, client, sign_in):
        sign_in(ALICE)
        client.get("/api/taxonomy")
        res = client.get("/api/taxonomy")
        assert len(res.json()["data"]["categories"]) == 7


class TestCrud:
    def test_add_rename_rule_edit_delete_round_trip(self, client, sign_in):
        sign_in(ALICE)
        client.get("/api/taxonomy")  # seed

        # add
        res = client.post(
            "/api/taxonomy",
            json={"name": "Receipts", "description": "Order receipts", "rule": "label_and_archive"},
        )
        assert res.status_code == 200
        cat = res.json()["data"]
        assert cat["name"] == "Receipts"
        assert cat["rule"] == "label_and_archive"
        assert cat["is_needs_review"] is False
        row = next(r for r in _categories(ALICE) if r.id == cat["id"])
        assert row.rule == "label_and_archive"
        assert row.position == 7  # appended after the 7 seeds

        # rename + description edit
        res = client.patch(
            f"/api/taxonomy/{cat['id']}",
            json={"name": "Purchases", "description": "All purchase mail"},
        )
        assert res.status_code == 200
        assert res.json()["data"]["name"] == "Purchases"
        row = next(r for r in _categories(ALICE) if r.id == cat["id"])
        assert row.name == "Purchases"
        assert row.description == "All purchase mail"

        # rule change
        res = client.patch(f"/api/taxonomy/{cat['id']}", json={"rule": "label_only"})
        assert res.json()["data"]["rule"] == "label_only"
        assert next(r for r in _categories(ALICE) if r.id == cat["id"]).rule == "label_only"

        # delete (unused)
        res = client.delete(f"/api/taxonomy/{cat['id']}")
        assert res.status_code == 200
        assert res.json()["data"]["deleted"] == cat["id"]
        assert all(r.id != cat["id"] for r in _categories(ALICE))

    def test_rename_resets_the_lazily_created_gmail_label(self, client, sign_in):
        sign_in(ALICE)
        cats = client.get("/api/taxonomy").json()["data"]["categories"]
        travel = next(c for c in cats if c["name"] == "Travel")
        with _db() as session:
            from db.models import Category

            session.get(Category, travel["id"]).gmail_label_id = "Label_123"
            session.commit()

        client.patch(f"/api/taxonomy/{travel['id']}", json={"name": "Trips"})
        row = next(r for r in _categories(ALICE) if r.id == travel["id"])
        assert row.name == "Trips"
        assert row.gmail_label_id is None  # recreated as ZI/Trips on next use


# --- edge cases --------------------------------------------------------------


class TestValidation:
    @pytest.mark.parametrize("name", ["", "   ", None, 42, "x" * 61])
    def test_bad_name_is_422(self, client, sign_in, name):
        sign_in(ALICE)
        res = client.post("/api/taxonomy", json={"name": name})
        assert res.status_code == 422
        body = res.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "validation_error"

    def test_bad_rule_is_422(self, client, sign_in):
        sign_in(ALICE)
        res = client.post("/api/taxonomy", json={"name": "X", "rule": "delete_forever"})
        assert res.status_code == 422
        assert "label_only" in res.json()["error"]["message"]

    def test_duplicate_name_case_insensitive_is_409(self, client, sign_in):
        sign_in(ALICE)
        client.get("/api/taxonomy")
        res = client.post("/api/taxonomy", json={"name": "finance"})
        assert res.status_code == 409
        assert res.json()["error"]["code"] == "conflict"

    def test_rename_to_existing_name_is_409(self, client, sign_in):
        sign_in(ALICE)
        cats = client.get("/api/taxonomy").json()["data"]["categories"]
        travel = next(c for c in cats if c["name"] == "Travel")
        res = client.patch(f"/api/taxonomy/{travel['id']}", json={"name": "Finance"})
        assert res.status_code == 409

    def test_rename_to_own_name_is_allowed(self, client, sign_in):
        sign_in(ALICE)
        cats = client.get("/api/taxonomy").json()["data"]["categories"]
        travel = next(c for c in cats if c["name"] == "Travel")
        assert (
            client.patch(f"/api/taxonomy/{travel['id']}", json={"name": "Travel"}).status_code
            == 200
        )


# --- error paths -------------------------------------------------------------


def _decide_with(user_id: str, category_id: str) -> None:
    """Write a ThreadDecision referencing the category, making it 'in use'."""
    from db.models import Run, ThreadDecision, User

    with _db() as session:
        session.add(User(id=user_id, email=f"{user_id}@example.com"))
        run = Run(id=f"{user_id}-run", user_id=user_id, status="completed")
        session.add(run)
        session.add(
            ThreadDecision(
                id=f"{user_id}-dec",
                user_id=user_id,
                run_id=run.id,
                gmail_thread_id=f"{user_id}-thread-1",
                sender="news@example.com",
                category_id=category_id,
                confidence=0.9,
                reason="test decision",
            )
        )
        session.commit()


class TestErrorPaths:
    def test_delete_in_use_category_is_a_clear_409(self, client, sign_in):
        sign_in(ALICE)
        cats = client.get("/api/taxonomy").json()["data"]["categories"]
        finance = next(c for c in cats if c["name"] == "Finance")
        _decide_with(ALICE, finance["id"])

        res = client.delete(f"/api/taxonomy/{finance['id']}")
        assert res.status_code == 409
        err = res.json()["error"]
        assert err["code"] == "conflict"
        assert "cannot be deleted" in err["message"]
        # Still there.
        assert any(r.id == finance["id"] for r in _categories(ALICE))

    def test_needs_review_is_not_deletable(self, client, sign_in):
        sign_in(ALICE)
        cats = client.get("/api/taxonomy").json()["data"]["categories"]
        nr = next(c for c in cats if c["is_needs_review"])
        res = client.delete(f"/api/taxonomy/{nr['id']}")
        assert res.status_code == 409
        assert "reserved" in res.json()["error"]["message"]

    def test_needs_review_rule_is_fixed_but_rename_is_fine(self, client, sign_in):
        sign_in(ALICE)
        cats = client.get("/api/taxonomy").json()["data"]["categories"]
        nr = next(c for c in cats if c["is_needs_review"])
        res = client.patch(f"/api/taxonomy/{nr['id']}", json={"rule": "label_and_archive"})
        assert res.status_code == 409
        # Setting it to its (fixed) value is a no-op, not an error.
        assert client.patch(f"/api/taxonomy/{nr['id']}", json={"rule": "label_only"}).status_code == 200
        # Renaming the reserved category is allowed; it stays reserved.
        res = client.patch(f"/api/taxonomy/{nr['id']}", json={"name": "Triage me"})
        assert res.status_code == 200
        assert res.json()["data"]["is_needs_review"] is True

    def test_merge_into_is_refused_in_phase_1(self, client, sign_in):
        sign_in(ALICE)
        cats = client.get("/api/taxonomy").json()["data"]["categories"]
        travel = next(c for c in cats if c["name"] == "Travel")
        finance = next(c for c in cats if c["name"] == "Finance")
        res = client.delete(f"/api/taxonomy/{travel['id']}?merge_into={finance['id']}")
        assert res.status_code == 409
        assert "Phase 2" in res.json()["error"]["message"]
        assert any(r.id == travel["id"] for r in _categories(ALICE))

    def test_unknown_category_is_404(self, client, sign_in):
        sign_in(ALICE)
        assert client.patch("/api/taxonomy/nope", json={"name": "X"}).status_code == 404
        assert client.delete("/api/taxonomy/nope").status_code == 404

    def test_unauthenticated_is_401_signed_out(self, client):
        for call in (
            lambda: client.get("/api/taxonomy"),
            lambda: client.post("/api/taxonomy", json={"name": "X"}),
            lambda: client.patch("/api/taxonomy/whatever", json={"name": "X"}),
            lambda: client.delete("/api/taxonomy/whatever"),
        ):
            res = call()
            assert res.status_code == 401
            body = res.json()
            assert body["ok"] is False
            assert body["error"]["code"] == "signed_out"


class TestPerUserIsolation:
    def test_users_see_and_touch_only_their_own_taxonomy(self, client, sign_in):
        sign_in(ALICE)
        alice_cats = client.get("/api/taxonomy").json()["data"]["categories"]
        alice_finance = next(c for c in alice_cats if c["name"] == "Finance")

        sign_in(BOB)
        bob_cats = client.get("/api/taxonomy").json()["data"]["categories"]
        assert {c["id"] for c in bob_cats}.isdisjoint({c["id"] for c in alice_cats})

        # Bob cannot edit or delete Alice's category — 404, not 403, so
        # existence is never leaked across tenants.
        assert (
            client.patch(
                f"/api/taxonomy/{alice_finance['id']}", json={"name": "Hacked"}
            ).status_code
            == 404
        )
        assert client.delete(f"/api/taxonomy/{alice_finance['id']}").status_code == 404
        assert (
            next(r for r in _categories(ALICE) if r.id == alice_finance["id"]).name
            == "Finance"
        )

        # Bob can create a name Alice already uses — per-user namespace.
        assert client.post("/api/taxonomy", json={"name": "Finance 2"}).status_code == 200


class TestWiringSeam:
    def test_router_carries_exactly_the_spec_routes(self):
        """The seam contract for src/api/app.py (owned by another slice):
        ``api.taxonomy.router`` exists with exactly the spec/api.md routes."""
        from api import taxonomy

        paths = {(r.path, m) for r in taxonomy.router.routes for m in r.methods}
        assert paths == {
            ("/api/taxonomy", "GET"),
            ("/api/taxonomy", "POST"),
            ("/api/taxonomy/{category_id}", "PATCH"),
            ("/api/taxonomy/{category_id}", "DELETE"),
        }
