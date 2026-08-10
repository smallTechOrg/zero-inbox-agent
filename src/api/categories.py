"""Taxonomy management — categories map 1:1 to real Gmail labels.

spec/api.md § Phase 2:
    POST   /api/categories                 create a category (creates the Gmail label)
    PATCH  /api/categories/{id}             edit a category (renames the Gmail label)
    POST   /api/categories/sync-labels      ensures every category has a real label (1:1)

``GET /api/categories`` is already served by ``api/triage.py`` (Phase 1) — not
duplicated here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api._common import VALIDATION_ERROR, api_error, not_found, ok
from api.session import require_user_id
from db.session import get_session
from tools.rules import VALID_ACTIONS
from tools.taxonomy import TaxonomyError, create_category, sync_labels, update_category

router = APIRouter()


class CategoryCreate(BaseModel):
    key: str
    name: str
    description: str = ""
    default_action: str = "keep"
    sort_order: int = 0


class CategoryUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    default_action: str | None = None
    sort_order: int | None = None


def _category_payload(category) -> dict:
    return {
        "id": category.id,
        "key": category.key,
        "name": category.name,
        "description": category.description,
        "channel_label_name": category.channel_label_name,
        "channel_label_id": category.channel_label_id,
        "default_action": category.default_action,
        "is_default": bool(category.is_default),
        "sort_order": category.sort_order,
    }


@router.post("/api/categories")
def create_category_route(
    body: CategoryCreate,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    if body.default_action not in VALID_ACTIONS:
        raise api_error(VALIDATION_ERROR, f"default_action must be one of {list(VALID_ACTIONS)}")
    try:
        category = create_category(
            session,
            user_id,
            key=body.key,
            name=body.name,
            description=body.description,
            default_action=body.default_action,
            sort_order=body.sort_order,
        )
    except TaxonomyError as exc:
        raise api_error(VALIDATION_ERROR, str(exc)) from exc
    return ok(_category_payload(category))


@router.patch("/api/categories/{category_id}")
def update_category_route(
    category_id: str,
    body: CategoryUpdate,
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    if body.default_action is not None and body.default_action not in VALID_ACTIONS:
        raise api_error(VALIDATION_ERROR, f"default_action must be one of {list(VALID_ACTIONS)}")
    try:
        category = update_category(
            session,
            user_id,
            category_id,
            name=body.name,
            description=body.description,
            default_action=body.default_action,
            sort_order=body.sort_order,
        )
    except LookupError as exc:
        raise not_found("Category") from exc
    except TaxonomyError as exc:
        raise api_error(VALIDATION_ERROR, str(exc)) from exc
    return ok(_category_payload(category))


def _label_manager_for_user(session: Session, user_id: str):
    """Builds a GmailLabelManager for the user's connected mailbox.

    Mirrors ``channels.gmail.adapter.GmailAdapter.for_refresh_token`` so this
    module has no import-time coupling to the adapter and stays independent of
    the parallel mutations slice.
    """
    from googleapiclient.discovery import build

    from channels.gmail.labels import GmailLabelManager
    from channels.gmail.oauth import credentials_from_refresh_token, google_oauth_config
    from channels.gmail.store import SqlConnectionStore
    from db.models import ChannelAccount

    account = (
        session.query(ChannelAccount)
        .filter(ChannelAccount.user_id == user_id)
        .order_by(ChannelAccount.connected_at.desc())
        .first()
    )
    if account is None:
        raise api_error(VALIDATION_ERROR, "no connected mailbox to sync labels against")

    refresh_token = SqlConnectionStore().load_refresh_token(
        user_id=user_id, connection_id=account.id
    )
    config = google_oauth_config()
    credentials = credentials_from_refresh_token(config, refresh_token)
    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    return GmailLabelManager(service)


@router.post("/api/categories/sync-labels")
def sync_labels_route(
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    label_manager = _label_manager_for_user(session, user_id)
    results = sync_labels(session, user_id, label_manager)
    return ok({"results": results})


@router.post("/api/categories/propose")
def propose_taxonomy(
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    """LLM-proposed taxonomy improvements based on the user's actual mail patterns."""
    import json

    from db.models import Category, Decision, Item
    from llm.client import get_llm_client

    # Current taxonomy
    categories = session.execute(
        select(Category)
        .where(Category.user_id == user_id)
        .order_by(Category.sort_order, Category.name)
    ).scalars().all()

    # Sample of recent decisions with their categories + subjects (last 200)
    rows = session.execute(
        select(Decision, Item, Category)
        .join(Item, Item.id == Decision.item_id)
        .outerjoin(Category, Category.id == Decision.category_id)
        .where(Decision.user_id == user_id)
        .order_by(Item.internal_date.desc())
        .limit(200)
    ).all()

    # Build prompt context
    taxonomy_text = "\n".join(
        f"- {c.name} ({c.key}): {c.description or 'no description'}"
        for c in categories
    )
    sample_lines = []
    for decision, item, category in rows[:50]:  # cap at 50 for prompt length
        cat_name = category.name if category else "uncategorized"
        sample_lines.append(f"  [{cat_name}] {item.subject[:80]}")
    sample_text = "\n".join(sample_lines) or "(no decisions yet)"

    prompt = f"""You are helping a user improve their email inbox taxonomy.

Current categories:
{taxonomy_text or "(none defined)"}

Sample of recently triaged emails (category → subject):
{sample_text}

Propose improvements to this taxonomy. You may suggest:
- New categories to add (if you see uncovered patterns)
- Categories to merge (if two are redundant)
- Better names or descriptions for existing categories
- Categories to remove (if they seem unused or too granular)

Respond with a JSON array of proposals. Each proposal has:
- "action": "add" | "rename" | "merge" | "remove" | "redescribe"
- "key": short_snake_case_key (for the category)
- "name": display name
- "description": one-sentence purpose
- "reasoning": why you suggest this (1-2 sentences)
- "merge_keys": [list of keys to merge] (only for "merge" action)

Return ONLY the JSON array, no prose."""

    client = get_llm_client()
    result = client.call_model_sync(
        prompt,
        system="You are a helpful assistant that analyzes email patterns and proposes inbox taxonomy improvements. Respond only with valid JSON.",
        json_schema={
            "type": "array",
            "items": {
                "type": "object",
                "required": ["action", "key", "name", "description", "reasoning"],
                "properties": {
                    "action": {"type": "string", "enum": ["add", "rename", "merge", "remove", "redescribe"]},
                    "key": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "reasoning": {"type": "string"},
                    "merge_keys": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    )

    try:
        proposals = json.loads(result.text)
    except (json.JSONDecodeError, TypeError):
        proposals = []

    return ok({"proposals": proposals, "model": result.model, "tokens": result.tokens_in + result.tokens_out})


@router.get("/api/inbox-summary")
def inbox_summary(
    user_id: str = Depends(require_user_id),
    session: Session = Depends(get_session),
) -> dict:
    """Live counts, straight from Gmail — how close to zero the inbox actually
    is right now, and how much sits under each category label.

    Each count is one ``labels().get()`` call (Gmail returns ``threadsTotal``
    directly), never a listing — cheap even with a dozen categories.
    """
    from db.models import Category, Decision

    label_manager = _label_manager_for_user(session, user_id)
    inbox_total = label_manager.label_count("INBOX")

    categories = session.execute(
        select(Category)
        .where(Category.user_id == user_id)
        .order_by(Category.sort_order, Category.name)
    ).scalars().all()

    by_category = []
    for category in categories:
        count = label_manager.label_count(category.channel_label_id) if category.channel_label_id else 0
        by_category.append(
            {
                "key": category.key,
                "name": category.name,
                "count": count,
                "channel_label_name": category.channel_label_name,
            }
        )

    # Scoped to the latest run only — a bare `status == needs_your_call` filter
    # counts every decision ever made across every historical run, which
    # inflates without bound as the same inbox gets re-triaged over time.
    from api.triage import _latest_run_id

    latest_run_id = _latest_run_id(session, user_id)
    needs_your_call = (
        session.execute(
            select(func.count())
            .select_from(Decision)
            .where(
                Decision.user_id == user_id,
                Decision.run_id == latest_run_id,
                Decision.status == "needs_your_call",
            )
        ).scalar_one()
        if latest_run_id
        else 0
    )

    return ok(
        {
            "inbox_total": inbox_total,
            "needs_your_call": needs_your_call,
            "categories": by_category,
        }
    )
