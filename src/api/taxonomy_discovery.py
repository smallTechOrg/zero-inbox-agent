"""Inbox-derived taxonomy — discover, approve, and delete safely.

spec/capabilities/inbox-derived-taxonomy.md::

    POST   /api/taxonomy/discover      propose a taxonomy from the user's own mail
                                       (MUTATES NOTHING)
    POST   /api/taxonomy/apply         approve: create/rename categories, mint the
                                       mined tier-1 rules, return the diff
    DELETE /api/categories/{id}        delete a category — refused while anything
                                       still references it

``discover`` is deliberately side-effect free. The user sees a proposal built from
their real senders — Apple, Google, Facebook, PayPal and BookMyShow by name, with
thread counts and the senders each category will absorb — and nothing is created,
renamed, deleted or archived until they approve.

The ``DELETE`` route lives here rather than in ``api/categories.py`` because it is
inseparable from the usage check next to it: a category is never removed on the
strength of it *looking* empty. That is precisely how ``e2e-actions-test`` would
have been "cleaned up" and how something real could have been detached instead.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from api._common import VALIDATION_ERROR, api_error, not_found, ok
from api.categories_usage import category_usage
from api.session import require_user_id
from db.session import get_session
from tools import taxonomy_discovery as discovery
from tools.taxonomy import TaxonomyError, create_category, update_category

router = APIRouter()


class ProposedCategory(BaseModel):
    key: str
    name: str
    description: str = ""
    default_action: str = "keep"
    rationale: str = ""
    evidence_senders: list[str] = Field(default_factory=list)
    covered_threads: int = 0


class TaxonomyApply(BaseModel):
    #: The proposal AS EDITED BY THE USER. The server never re-derives it here —
    #: silently re-running discovery on approve would throw away the user's edits,
    #: which is the one thing an approval screen must never do.
    proposal: list[ProposedCategory]
    #: Approving mints the deterministic tier-1 rules from the evidence lists.
    #: Off means "keep my categories, keep asking the model" — supported, but it
    #: gives up the whole cost and confidence win, so it is opt-out, not opt-in.
    mine_rules: bool = True


def _discover(session: Session, user_id: str) -> dict:
    census = discovery.build_census(session, user_id=user_id)
    gap_set = discovery.build_gap_set(session, user_id=user_id)
    result = discovery.propose_taxonomy(
        session, user_id=user_id, census=census, gap_set=gap_set
    )
    result["census_size"] = len(census)
    result["gap_set_size"] = len(gap_set)
    return result


@router.post("/api/taxonomy/discover")
def discover_route(
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    """Propose a taxonomy derived from this user's mail. Mutates nothing."""
    result = _discover(session, user_id)
    # A preview of exactly which senders would stop needing the model, so the
    # approve screen can state the win rather than promise it.
    result["mined_rule_preview"] = discovery.mine_sender_rules(
        discovery.build_census(session, user_id=user_id), result["proposal"]
    )
    return ok(result)


@router.post("/api/taxonomy/apply")
def apply_route(
    body: TaxonomyApply,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    """Approve a proposal: create/rename categories, then mint the mined rules."""
    from db.models import Category

    if not body.proposal:
        raise api_error(VALIDATION_ERROR, "proposal is empty — nothing to apply")

    existing = {
        row.key: row
        for row in session.execute(
            select(Category).where(Category.user_id == user_id)
        ).scalars()
    }

    diff: list[dict] = []
    try:
        for order, proposed in enumerate(body.proposal):
            current = existing.get(proposed.key)
            if current is None:
                create_category(
                    session,
                    user_id,
                    key=proposed.key,
                    name=proposed.name,
                    description=proposed.description,
                    default_action=proposed.default_action,
                    sort_order=order,
                )
                diff.append({"key": proposed.key, "change": "added", "name": proposed.name})
                continue
            changed = (
                current.name != proposed.name
                or (current.description or "") != proposed.description
                or current.default_action != proposed.default_action
            )
            update_category(
                session,
                user_id,
                current.id,
                name=proposed.name,
                description=proposed.description,
                default_action=proposed.default_action,
                sort_order=order,
            )
            diff.append(
                {
                    "key": proposed.key,
                    "change": "updated" if changed else "kept",
                    "name": proposed.name,
                }
            )
    except TaxonomyError as exc:
        # NEVER_ARCHIVE_KEYS lands here. The whole apply is refused rather than
        # partially written: a half-applied taxonomy is not a state the user can
        # reason about or undo.
        session.rollback()
        raise api_error(VALIDATION_ERROR, str(exc)) from exc

    minted = {"created": 0, "updated": 0, "skipped_user": 0}
    if body.mine_rules:
        census = discovery.build_census(session, user_id=user_id)
        try:
            rules = discovery.mine_sender_rules(
                census, [p.model_dump() for p in body.proposal]
            )
            minted = discovery.materialise_rules(session, user_id=user_id, rules=rules)
        except (TaxonomyError, discovery.DiscoveryError) as exc:
            session.rollback()
            raise api_error(VALIDATION_ERROR, str(exc)) from exc

    # A material taxonomy change means every past decision was filed under a
    # taxonomy that no longer exists. Saying so is the honest thing; the
    # re-organisation itself is the user's call, never automatic.
    material = any(d["change"] in ("added", "updated") for d in diff)

    return ok(
        {
            "diff": diff,
            "mined_rules": minted,
            "reorganise_recommended": material,
            "reorganise_reason": (
                "Your categories changed, so your past decisions were filed under a "
                "taxonomy that no longer matches. Re-organising re-files them."
            )
            if material
            else None,
        }
    )


@router.delete("/api/categories/{category_id}")
def delete_category_route(
    category_id: str,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    """Delete a category. **Refused** while any decision, rule or item references it.

    Deleting a category never deletes its Gmail label and never deletes mail —
    the label is left exactly where it is, so nothing the user can see disappears.
    """
    from tools.taxonomy import delete_category

    try:
        usage = category_usage(session, user_id=user_id, category_id=category_id)
    except LookupError as exc:
        raise not_found("Category") from exc

    if not usage["safe_to_delete"]:
        raise api_error(
            VALIDATION_ERROR,
            f"{usage['name']!r} is still in use — {usage['decisions']} decisions, "
            f"{usage['items']} threads and {usage['rules']} rules reference it. "
            "Nothing was deleted. Re-file or delete those first.",
        )

    delete_category(session, user_id, category_id)
    return ok({"deleted": True, "category_id": category_id, "usage": usage})
