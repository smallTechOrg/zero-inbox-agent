"""Taxonomy CRUD — spec/api.md § Audit & Taxonomy (Phase 1).

Routes (all scoped to the signed-in user):

* ``GET    /api/taxonomy``       — categories with rules, ordered; lazily seeds
  the defaults for a user whose taxonomy is empty (idempotent).
* ``POST   /api/taxonomy``       — add ``{name, description?, rule?}``.
* ``PATCH  /api/taxonomy/{id}``  — rename / edit description / change rule.
* ``DELETE /api/taxonomy/{id}``  — delete-if-unused (409 if in use). "Needs
  review" is reserved: not deletable, rule fixed ``label_only`` (409 on both).
  ``?merge_into=`` is Phase 2 and is refused loudly, never silently ignored.

Auth: delegates to the ``api/session.py`` chokepoint (auth-gmail slice) at
request time via :func:`current_user_id`, so this module has no import-time
dependency on that slice's internals — only on the contract
``require_user_id(request[, response]) -> str``.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api._common import (
    CONFLICT,
    VALIDATION_ERROR,
    api_error,
    iso,
    not_found,
    ok,
    signed_out,
)
from db.models import Category, SenderProfile, ThreadDecision
from db.seed import VALID_RULES, ensure_default_taxonomy
from db.session import get_session

router = APIRouter()

#: Falls back to the spec'd cookie name if the auth slice hasn't landed yet.
_DEFAULT_COOKIE_NAME = "zi_session"

MAX_NAME_LEN = 60
MAX_DESCRIPTION_LEN = 500


def current_user_id(request: Request, response: Response) -> str:
    """The per-user scope guard: cookie → user id, else 401 ``signed_out``.

    Delegates to ``api.session.require_user_id`` (the single auth chokepoint).
    A request with no session cookie at all is refused here without touching
    that module, so an unauthenticated 401 never depends on slice landing order.
    """
    try:
        import api.session as session_api  # request-time: contract, not internals
    except Exception:  # the auth slice has not landed — refuse, never guess
        if not request.cookies.get(_DEFAULT_COOKIE_NAME):
            raise signed_out() from None
        raise

    cookie_name = getattr(session_api, "SESSION_COOKIE_NAME", None) or getattr(
        session_api, "COOKIE_NAME", _DEFAULT_COOKIE_NAME
    )
    if not request.cookies.get(cookie_name):
        raise signed_out()
    impl = session_api.require_user_id
    try:
        return impl(request, response)
    except TypeError:
        return impl(request)


def _serialize(cat: Category) -> dict:
    return {
        "id": cat.id,
        "name": cat.name,
        "description": cat.description,
        "rule": cat.rule,
        "gmail_label_id": cat.gmail_label_id,
        "is_needs_review": cat.is_needs_review,
        "position": cat.position,
        "created_at": iso(cat.created_at),
    }


def _clean_name(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise api_error(VALIDATION_ERROR, "name must be a non-empty string")
    name = re.sub(r"\s+", " ", value.strip())
    if len(name) > MAX_NAME_LEN:
        raise api_error(VALIDATION_ERROR, f"name must be at most {MAX_NAME_LEN} characters")
    return name


def _clean_description(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise api_error(VALIDATION_ERROR, "description must be a string")
    description = value.strip()
    if len(description) > MAX_DESCRIPTION_LEN:
        raise api_error(
            VALIDATION_ERROR,
            f"description must be at most {MAX_DESCRIPTION_LEN} characters",
        )
    return description


def _clean_rule(value) -> str:
    if value not in VALID_RULES:
        raise api_error(
            VALIDATION_ERROR, f"rule must be one of: {', '.join(VALID_RULES)}"
        )
    return value


def _assert_name_free(
    session: Session, user_id: str, name: str, *, exclude_id: str | None = None
) -> None:
    query = select(Category.id).where(
        Category.user_id == user_id,
        func.lower(Category.name) == name.lower(),
    )
    if exclude_id is not None:
        query = query.where(Category.id != exclude_id)
    if session.execute(query).first() is not None:
        raise api_error(CONFLICT, f'A category named "{name}" already exists.')


def _get_owned(session: Session, user_id: str, category_id: str) -> Category:
    cat = session.get(Category, category_id)
    if cat is None or cat.user_id != user_id:
        # Another user's category is indistinguishable from a missing one.
        raise not_found("category")
    return cat


@router.get("/api/taxonomy")
def list_taxonomy(
    user_id: str = Depends(current_user_id),
    session: Session = Depends(get_session),
) -> dict:
    """Ordered categories with rules; seeds the defaults for an empty taxonomy."""
    if ensure_default_taxonomy(session, user_id):
        session.commit()

    rows = session.execute(
        select(Category)
        .where(Category.user_id == user_id)
        .order_by(Category.position, Category.name)
    ).scalars().all()
    return ok({"categories": [_serialize(c) for c in rows]})


@router.post("/api/taxonomy")
def add_category(
    body: dict,
    user_id: str = Depends(current_user_id),
    session: Session = Depends(get_session),
) -> dict:
    name = _clean_name(body.get("name"))
    description = _clean_description(body.get("description"))
    rule = _clean_rule(body.get("rule", "label_only"))

    _assert_name_free(session, user_id, name)

    max_position = session.execute(
        select(func.max(Category.position)).where(Category.user_id == user_id)
    ).scalar()
    cat = Category(
        user_id=user_id,
        name=name,
        description=description,
        rule=rule,
        is_needs_review=False,
        position=(max_position if max_position is not None else -1) + 1,
    )
    session.add(cat)
    session.commit()
    return ok(_serialize(cat))


@router.patch("/api/taxonomy/{category_id}")
def edit_category(
    category_id: str,
    body: dict,
    user_id: str = Depends(current_user_id),
    session: Session = Depends(get_session),
) -> dict:
    cat = _get_owned(session, user_id, category_id)

    if "rule" in body:
        rule = _clean_rule(body["rule"])
        if cat.is_needs_review and rule != "label_only":
            raise api_error(
                CONFLICT,
                '"Needs review" is reserved: its rule is fixed to label_only.',
            )
        cat.rule = rule

    if "name" in body:
        name = _clean_name(body["name"])
        _assert_name_free(session, user_id, name, exclude_id=cat.id)
        cat.name = name
        # The ZI/ Gmail label follows the name; it is (re)created lazily on
        # first use by the run, so a stale label id must not be reused.
        cat.gmail_label_id = None

    if "description" in body:
        cat.description = _clean_description(body["description"])

    session.commit()
    return ok(_serialize(cat))


@router.delete("/api/taxonomy/{category_id}")
def delete_category(
    category_id: str,
    merge_into: str | None = None,
    user_id: str = Depends(current_user_id),
    session: Session = Depends(get_session),
) -> dict:
    cat = _get_owned(session, user_id, category_id)

    if cat.is_needs_review:
        raise api_error(CONFLICT, '"Needs review" is reserved and cannot be deleted.')

    if merge_into is not None:
        # spec/api.md: merge-on-delete arrives in Phase 2. Refused loudly.
        raise api_error(
            CONFLICT,
            "Merging categories arrives in Phase 2 — Phase 1 supports "
            "delete-if-unused only.",
        )

    in_use = int(
        session.execute(
            select(func.count(ThreadDecision.id)).where(
                ThreadDecision.user_id == user_id,
                ThreadDecision.category_id == cat.id,
            )
        ).scalar_one()
        or 0
    ) + int(
        session.execute(
            select(func.count(SenderProfile.id)).where(
                SenderProfile.user_id == user_id,
                SenderProfile.category_id == cat.id,
            )
        ).scalar_one()
        or 0
    )
    if in_use:
        raise api_error(
            CONFLICT,
            f'"{cat.name}" is still referenced by {in_use} record'
            f'{"s" if in_use != 1 else ""} (decided threads or sender profiles) '
            "and cannot be deleted in Phase 1. Merge-on-delete arrives in Phase 2.",
        )

    session.delete(cat)
    session.commit()
    return ok({"deleted": category_id})
