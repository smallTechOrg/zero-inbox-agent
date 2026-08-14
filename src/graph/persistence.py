"""DB access for the triage graph.

Only ``load_context`` reads and only ``persist_run_results`` writes — no other node
touches the database, so parallel LLM branches never contend for a connection.

Model classes are resolved from the SQLAlchemy registry **by table name** (see
``spec/data.md``) rather than by import name, and every write only sets columns that
actually exist on the mapped class. Column expressions are used in all filters — never
raw SQL strings — so the queries stay dialect-safe.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from tools.rules import DEFAULT_TAXONOMY

DEFAULT_CONFIDENCE_FLOOR = 0.75
DEFAULT_AUTO_ACT_THRESHOLD = 0.95


class SchemaMissing(RuntimeError):
    """A table required by the triage graph is absent from the model registry."""


def _registry() -> dict[str, type]:
    from db.models import Base

    return {
        mapper.class_.__tablename__: mapper.class_
        for mapper in Base.registry.mappers
        if hasattr(mapper.class_, "__tablename__")
    }


def model_for(table: str) -> type | None:
    return _registry().get(table)


def require_model(table: str) -> type:
    cls = model_for(table)
    if cls is None:
        raise SchemaMissing(
            f"No mapped class for table {table!r}. The triage graph requires the "
            "Phase 1 schema from spec/data.md to be present in src/db/models.py."
        )
    return cls


def _columns(cls: type) -> set[str]:
    from sqlalchemy import inspect as sa_inspect

    return {attr.key for attr in sa_inspect(cls).mapper.column_attrs}


def _assign(obj: Any, cls: type, values: dict) -> Any:
    allowed = _columns(cls)
    for key, value in values.items():
        if key in allowed:
            setattr(obj, key, value)
    return obj


def _new(cls: type, values: dict) -> Any:
    return _assign(cls(), cls, values)


def _rows(session: Session, cls: type, **filters: Any) -> list[Any]:
    stmt = select(cls)
    for key, value in filters.items():
        stmt = stmt.where(getattr(cls, key) == value)
    return list(session.execute(stmt).scalars())


# --------------------------------------------------------------------------- read


def load_context(session: Session, user_id: str) -> dict:
    """Taxonomy, active rules, sender stats and settings for one user."""
    categories: list[dict] = []
    category_cls = model_for("categories")
    if category_cls is not None:
        # Backfill safety net: a user whose default taxonomy was never seeded
        # (e.g. connected before seeding existed) gets it on their next run, so
        # decisions.category_id can always resolve. Idempotent — never duplicates.
        from db.seed import ensure_default_taxonomy

        ensure_default_taxonomy(session, user_id)
        for row in _rows(session, category_cls, user_id=user_id):
            categories.append(
                {
                    "id": getattr(row, "id", None),
                    "key": row.key,
                    "name": getattr(row, "name", row.key),
                    "description": getattr(row, "description", "") or "",
                    "default_action": getattr(row, "default_action", "keep"),
                }
            )
    if not categories:
        categories = [dict(c) for c in DEFAULT_TAXONOMY]

    rules: list[dict] = []
    rule_cls = model_for("rules")
    if rule_cls is not None:
        for row in _rows(session, rule_cls, user_id=user_id):
            if getattr(row, "status", "active") not in ("active", "automatic"):
                continue
            rules.append(
                {
                    "id": row.id,
                    "name": getattr(row, "name", row.id),
                    "matcher": getattr(row, "matcher", None) or {},
                    "action": getattr(row, "action", None) or {},
                    "status": getattr(row, "status", "active"),
                    "confidence": float(getattr(row, "confidence", 0.9) or 0.9),
                }
            )

    sender_stats: dict[str, dict] = {}
    sender_cls = model_for("sender_profiles")
    if sender_cls is not None:
        for row in _rows(session, sender_cls, user_id=user_id):
            sender_stats[(row.sender_email or "").lower()] = {
                "received_count": getattr(row, "received_count", 0) or 0,
                "opened_count": getattr(row, "opened_count", 0) or 0,
                "replied_count": getattr(row, "replied_count", 0) or 0,
                "archived_by_user_count": getattr(row, "archived_by_user_count", 0) or 0,
                "ever_replied": bool(getattr(row, "ever_replied", False)),
                "importance_score": float(getattr(row, "importance_score", 0.0) or 0.0),
            }

    settings = {
        "confidence_floor": DEFAULT_CONFIDENCE_FLOOR,
        "auto_act_threshold": DEFAULT_AUTO_ACT_THRESHOLD,
        "llm_model": None,
    }
    settings_cls = model_for("user_settings")
    if settings_cls is not None:
        rows = _rows(session, settings_cls, user_id=user_id)
        if rows:
            row = rows[0]
            settings.update(
                {
                    "confidence_floor": float(
                        getattr(row, "confidence_floor", DEFAULT_CONFIDENCE_FLOOR)
                        or DEFAULT_CONFIDENCE_FLOOR
                    ),
                    "auto_act_threshold": float(
                        getattr(row, "auto_act_threshold", DEFAULT_AUTO_ACT_THRESHOLD)
                        or DEFAULT_AUTO_ACT_THRESHOLD
                    ),
                    "llm_model": getattr(row, "llm_model", None) or None,
                }
            )

    vip: dict = {"emails": [], "domains": [], "keywords": []}
    vip_cls = model_for("vip_entries")
    if vip_cls is not None:
        for row in _rows(session, vip_cls, user_id=user_id):
            kind = getattr(row, "kind", "")
            value = getattr(row, "value", "")
            if kind == "email" and value:
                vip["emails"].append(value)
            elif kind == "domain" and value:
                vip["domains"].append(value)
            elif kind == "keyword" and value:
                vip["keywords"].append(value)

    priorities_profile = ""
    profile_cls = model_for("priority_profiles")
    if profile_cls is not None:
        row = session.get(profile_cls, user_id)
        if row is not None:
            priorities_profile = getattr(row, "text", "") or ""

    return {
        "categories": categories,
        "rules": rules,
        "sender_stats": sender_stats,
        "settings": settings,
        "vip": vip,
        "priorities_profile": priorities_profile,
    }


# -------------------------------------------------------------------------- write

_ITEM_FIELDS = (
    "external_thread_id",
    "external_message_ids",
    "subject",
    "from_name",
    "from_email",
    "from_domain",
    "to_emails",
    "cc_emails",
    "list_id",
    "unsubscribe_url",
    "message_count",
    "has_attachments",
    "snippet_redacted",
    "internal_date",
    "is_unread",
    "channel_labels",
)


def persist_sender_profiles(
    session: Session, *, user_id: str, signals: dict[str, dict]
) -> dict[str, str]:
    """Upsert per-sender evidence harvested from the channel.

    ``signals`` is keyed by lowercased sender email (the addresses the user has
    replied to, per ``ChannelAdapter.sender_history``). Each value is a dict with
    ``received_count``, ``opened_count``, ``replied_count``,
    ``archived_by_user_count``, ``ever_replied`` and ``last_replied_at``.

    A ``received_count`` derived from the current inbox pass is merged in so the
    never-miss reply-history signal (``ever_replied``) and the learned bulk-archive
    heuristic (received / opened / archived ratios) both reflect this run.
    Idempotent on ``(user_id, sender_email)``.
    """
    profile_cls = model_for("sender_profiles")
    if profile_cls is None:
        return {}
    existing = {
        row.sender_email: row
        for row in _rows(session, profile_cls, user_id=user_id)
    }
    id_map: dict[str, str] = {}
    for email, stats in (signals or {}).items():
        row = existing.get(email)
        values = {
            "sender_email": email,
            "sender_domain": email.split("@")[-1] if "@" in email else "",
            "last_seen_at": datetime.now(timezone.utc),
        }
        for field in (
            "received_count",
            "opened_count",
            "replied_count",
            "archived_by_user_count",
            "ever_replied",
            "last_replied_at",
        ):
            if field in stats and stats[field] is not None:
                values[field] = stats[field]
        # Compute the never-miss importance score when the caller hasn't supplied
        # one. A sender the user has replied to gets a high score (they must
        # never be auto-archived); everyone else defaults to a low score driven
        # by their archived_by_user ratio. Real harvests from sender_history()
        # only carry ever_replied / replied_count, so this keeps the production
        # signal non-zero for replied-to senders.
        if "importance_score" not in stats:
            ever_replied = bool(stats.get("ever_replied", False))
            replied = int(stats.get("replied_count", 0))
            archived = int(stats.get("archived_by_user_count", 0))
            received = int(stats.get("received_count", 0))
            if ever_replied or replied > 0:
                values["importance_score"] = round(
                    0.75 + 0.20 * (1 if ever_replied else 0) + 0.05 * min(replied, 5) / 5, 3
                )
            elif received > 0:
                values["importance_score"] = round(
                    0.10 * (1 - archived / received) if received else 0.1, 3
                )
            else:
                values["importance_score"] = 0.05
        if row is None:
            row = _new(profile_cls, values)
            row.id = str(uuid4())
            row.user_id = user_id
            session.add(row)
        else:
            _assign(row, profile_cls, values)
        session.flush()
        id_map[email] = row.id
    return id_map


def upsert_items(
    session: Session, *, user_id: str, channel_account_id: str, items: list[dict]
) -> dict[str, str]:
    """Persist the redacted item rows. Returns ``{state_item_id: db_item_id}``.

    Never writes body text — only the fields in ``_ITEM_FIELDS``, whose only
    content-bearing member is the already-redacted <=200 char snippet.
    """
    item_cls = require_model("items")
    existing = {
        row.external_thread_id: row
        for row in _rows(session, item_cls, user_id=user_id)
    }
    id_map: dict[str, str] = {}

    for item in items:
        thread_id = item.get("external_thread_id") or item["id"]
        values = {k: item.get(k) for k in _ITEM_FIELDS if k in item}
        values["external_thread_id"] = thread_id
        values["user_id"] = user_id
        values["channel_account_id"] = channel_account_id
        row = existing.get(thread_id)
        if row is None:
            row = _new(item_cls, values)
            if not getattr(row, "id", None):
                row.id = item["id"]
            session.add(row)
            existing[thread_id] = row
        else:
            _assign(row, item_cls, values)
        session.flush()
        id_map[item["id"]] = row.id

    return id_map


def already_decided_item_ids(session: Session, run_id: str) -> set[str]:
    """Every item id this run has already decided (Phase 6 resume).

    Returns BOTH the database item ids and their ``external_thread_id``s, because a
    graph state's ``item["id"]`` is the channel's thread id on first ingest but the
    persisted row id afterwards. ``fetch_items`` filters on either, so a resumed run
    never re-classifies a thread it already decided.
    """
    decision_cls = model_for("decisions")
    item_cls = model_for("items")
    if decision_cls is None:
        return set()
    decided = set(
        session.execute(
            select(decision_cls.item_id).where(decision_cls.run_id == run_id)
        ).scalars()
    )
    if not decided or item_cls is None:
        return {d for d in decided if d}
    externals = set(
        session.execute(
            select(item_cls.external_thread_id).where(item_cls.id.in_(sorted(decided)))
        ).scalars()
    )
    return {value for value in (decided | externals) if value}


def load_provisional_for_review(
    session: Session, *, run_id: str, user_id: str, only_item_ids: set[str] | None = None
) -> tuple[list[dict], list[dict]]:
    """Rehydrate a resumed run's already-persisted, not-yet-reviewed decisions.

    Returns ``(items, decisions)`` in the graph's in-memory shape. Rule B: threads
    decided by the interrupted leg are carried into the reviewer pass with the newly
    decided ones, so the whole run's archive proposals are reviewed exactly once and
    nothing is left permanently ``provisional``.
    """
    decision_cls = model_for("decisions")
    item_cls = model_for("items")
    category_cls = model_for("categories")
    if decision_cls is None or item_cls is None:
        return [], []
    rows = list(
        session.execute(
            select(decision_cls).where(
                decision_cls.run_id == run_id,
                decision_cls.review_state == "provisional",
            )
        ).scalars()
    )
    if not rows:
        return [], []
    item_rows = {
        row.id: row
        for row in session.execute(
            select(item_cls).where(item_cls.id.in_([r.item_id for r in rows]))
        ).scalars()
    }
    category_keys: dict[str, str] = {}
    if category_cls is not None:
        for row in _rows(session, category_cls, user_id=user_id):
            category_keys[row.id] = row.key

    items: list[dict] = []
    decisions: list[dict] = []
    for row in rows:
        item = item_rows.get(row.item_id)
        if item is None:
            continue
        if only_item_ids is not None and not (
            item.id in only_item_ids or item.external_thread_id in only_item_ids
        ):
            # This leg's own freshly checkpointed rows are already in state — carrying
            # them again would put the same item_id in the reviewer batch twice.
            continue
        items.append(
            {
                "id": item.id,
                "external_thread_id": item.external_thread_id,
                "subject": item.subject,
                "from_name": item.from_name,
                "from_email": item.from_email,
                "from_domain": item.from_domain,
                "list_id": item.list_id,
                "unsubscribe_url": item.unsubscribe_url,
                "snippet_redacted": item.snippet_redacted,
                "message_count": item.message_count,
                "has_attachments": item.has_attachments,
                "is_unread": item.is_unread,
                "internal_date": item.internal_date,
                "channel_labels": item.channel_labels,
            }
        )
        decisions.append(
            {
                "item_id": item.id,
                "category": category_keys.get(row.category_id or ""),
                "proposed_action": row.proposed_action,
                "confidence": float(row.confidence or 0.0),
                "reasoning": row.reasoning or "",
                "decided_by": row.decided_by,
                "rule_id": row.rule_id,
                "time_sensitive": bool(row.time_sensitive),
                "status": row.status,
                "unsure": False,
            }
        )
    return items, decisions


def run_cost_totals(session: Session, run_id: str) -> dict:
    """Cost accrued by a run **from the persisted llm_calls rows**.

    A resumed run's in-memory ``state["llm_calls"]`` only covers the resume, so cost
    must be summed from the database or a resume would reset the run's spend to the
    cost of its final leg (spec/capabilities/durable-resumable-runs.md, Rule B).
    """
    from sqlalchemy import func

    call_cls = model_for("llm_calls")
    if call_cls is None:
        return {"tokens_in": 0, "tokens_out": 0, "usd": 0.0, "llm_calls": 0}
    row = session.execute(
        select(
            func.coalesce(func.sum(call_cls.tokens_in), 0),
            func.coalesce(func.sum(call_cls.tokens_out), 0),
            func.coalesce(func.sum(call_cls.cost_usd), 0.0),
            func.count(call_cls.id),
        ).where(call_cls.run_id == run_id)
    ).one()
    return {
        "tokens_in": int(row[0] or 0),
        "tokens_out": int(row[1] or 0),
        "usd": round(float(row[2] or 0.0), 6),
        "llm_calls": int(row[3] or 0),
    }


def insert_provisional_decisions(
    session: Session,
    *,
    run_id: str,
    user_id: str,
    channel_account_id: str,
    items: list[dict],
    decisions: list[dict],
    llm_calls: list[dict] | None = None,
) -> int:
    """Insert one tier batch's decisions as ``review_state="provisional"``.

    Durable, not final. Skips any ``(run_id, item_id)`` that already exists — the
    UniqueConstraint is the idempotency guard, so a re-decided thread is never
    inserted twice and never double-counted. Returns the number of rows written.
    """
    if not decisions:
        return 0
    id_map = upsert_items(
        session, user_id=user_id, channel_account_id=channel_account_id, items=items
    )

    category_ids: dict[str, str] = {}
    category_cls = model_for("categories")
    if category_cls is not None:
        for row in _rows(session, category_cls, user_id=user_id):
            category_ids[row.key] = row.id

    decision_cls = require_model("decisions")
    existing = {
        row.item_id for row in _rows(session, decision_cls, run_id=run_id)
    }

    written = 0
    for decision in decisions:
        db_item_id = id_map.get(decision["item_id"])
        if db_item_id is None or db_item_id in existing:
            continue
        row = _new(
            decision_cls,
            {
                "user_id": user_id,
                "run_id": run_id,
                "item_id": db_item_id,
                "category_id": category_ids.get(decision.get("category") or ""),
                "proposed_action": decision["proposed_action"],
                "confidence": float(decision.get("confidence") or 0.0),
                "reasoning": decision.get("reasoning") or "",
                "decided_by": decision.get("decided_by") or "llm",
                "rule_id": decision.get("rule_id"),
                "time_sensitive": bool(decision.get("time_sensitive")),
                "status": decision.get("status", "proposed"),
                "review_state": "provisional",
            },
        )
        session.add(row)
        existing.add(db_item_id)
        written += 1

    for call in llm_calls or []:
        call_cls = model_for("llm_calls")
        if call_cls is None:
            break
        session.add(
            _new(
                call_cls,
                {
                    "user_id": user_id,
                    "run_id": run_id,
                    "purpose": call.get("purpose", "classify"),
                    "model": call.get("model", ""),
                    "items_in_batch": call.get("items_in_batch", 0),
                    "tokens_in": call.get("tokens_in", 0),
                    "tokens_out": call.get("tokens_out", 0),
                    "cost_usd": call.get("cost_usd", 0.0),
                    "latency_ms": call.get("latency_ms", 0),
                },
            )
        )

    session.flush()
    return written


def upgrade_review_state(
    session: Session,
    *,
    run_id: str,
    state: str = "reviewed",
    item_ids: list[str] | None = None,
    only_provisional: bool = True,
) -> int:
    """Move this run's decision rows onto the next never-miss review state.

    ``item_ids`` (database item ids) narrows the upgrade; omitted, every row of the
    run is upgraded. Returns the number of rows changed.
    """
    decision_cls = model_for("decisions")
    if decision_cls is None:
        return 0
    stmt = select(decision_cls).where(decision_cls.run_id == run_id)
    if only_provisional:
        stmt = stmt.where(decision_cls.review_state == "provisional")
    if item_ids is not None:
        if not item_ids:
            return 0
        stmt = stmt.where(decision_cls.item_id.in_(list(item_ids)))
    changed = 0
    for row in session.execute(stmt).scalars():
        row.review_state = state
        changed += 1
    session.flush()
    return changed


def finalise_review(
    session: Session,
    *,
    run_id: str,
    user_id: str,
    decisions: list[dict],
    review_failed_item_ids: list[str] | None = None,
) -> dict:
    """Write the reviewer's verdict onto the run's rows and upgrade ``review_state``.

    This is the moment a decision stops being provisional and becomes final. Rows the
    reviewer could not process become ``review_failed`` and are treated exactly like
    un-reviewed rows: ``apply_decision`` refuses them forever.

    Returns ``{"reviewed": n, "review_failed": n, "flipped": [state_item_id, ...]}``.
    """
    decision_cls = model_for("decisions")
    item_cls = model_for("items")
    if decision_cls is None or item_cls is None:
        return {"reviewed": 0, "review_failed": 0, "flipped": []}

    # Graph state ids are either the persisted item id or the channel thread id.
    state_ids = {d["item_id"] for d in decisions}
    db_id_of: dict[str, str] = {}
    if state_ids:
        for row in session.execute(
            select(item_cls).where(
                item_cls.user_id == user_id,
                (item_cls.id.in_(sorted(state_ids)))
                | (item_cls.external_thread_id.in_(sorted(state_ids))),
            )
        ).scalars():
            db_id_of[row.id] = row.id
            if row.external_thread_id:
                db_id_of[row.external_thread_id] = row.id

    failed_db_ids = {
        db_id_of[i] for i in (review_failed_item_ids or []) if i in db_id_of
    }
    by_db_id = {
        db_id_of[d["item_id"]]: d for d in decisions if d["item_id"] in db_id_of
    }

    reviewed = 0
    failed = 0
    flipped: list[str] = []
    for row in session.execute(
        select(decision_cls).where(decision_cls.run_id == run_id)
    ).scalars():
        decision = by_db_id.get(row.item_id)
        if decision is not None:
            if (
                row.proposed_action != decision["proposed_action"]
                or row.decided_by != decision.get("decided_by")
                or row.status != decision.get("status", row.status)
            ):
                flipped.append(decision["item_id"])
            _assign(
                row,
                decision_cls,
                {
                    "proposed_action": decision["proposed_action"],
                    "confidence": float(decision.get("confidence") or 0.0),
                    "reasoning": decision.get("reasoning") or "",
                    "decided_by": decision.get("decided_by") or row.decided_by,
                    "status": decision.get("status", row.status),
                },
            )
        if row.item_id in failed_db_ids:
            row.review_state = "review_failed"
            failed += 1
        elif row.review_state == "provisional":
            row.review_state = "reviewed"
            reviewed += 1
    session.flush()
    return {"reviewed": reviewed, "review_failed": failed, "flipped": flipped}


def persist_run_results(
    session: Session,
    *,
    run_id: str,
    user_id: str,
    channel_account_id: str,
    items: list[dict],
    decisions: list[dict],
    clusters: list[dict],
    counts: dict,
    cost: dict,
    llm_calls: list[dict],
    status: str,
    error: str | None = None,
) -> dict:
    """Write items, clusters, decisions and llm_calls for one run. Idempotent on
    ``(run_id, item_id)`` so a resumed run never decides a thread twice."""
    item_ids = upsert_items(
        session, user_id=user_id, channel_account_id=channel_account_id, items=items
    )

    category_ids: dict[str, str] = {}
    category_cls = model_for("categories")
    if category_cls is not None:
        # Same backfill safety net as load_context: category_id must resolve even
        # when items were supplied directly and load_context never ran seeding.
        from db.seed import ensure_default_taxonomy

        ensure_default_taxonomy(session, user_id)
        for row in _rows(session, category_cls, user_id=user_id):
            category_ids[row.key] = row.id

    cluster_cls = require_model("clusters")
    cluster_of_item: dict[str, str] = {}
    for spec in clusters:
        row = _new(
            cluster_cls,
            {
                "user_id": user_id,
                "run_id": run_id,
                "kind": spec["kind"],
                "label": spec["label"],
                "item_count": spec["item_count"],
                "suggested_action": spec["suggested_action"],
                "min_confidence": spec["min_confidence"],
                "avg_confidence": spec["avg_confidence"],
            },
        )
        session.add(row)
        session.flush()
        for state_item_id in spec["item_ids"]:
            cluster_of_item[state_item_id] = row.id

    decision_cls = require_model("decisions")
    existing_decisions = {
        row.item_id: row for row in _rows(session, decision_cls, run_id=run_id)
    }

    written = 0
    for decision in decisions:
        db_item_id = item_ids.get(decision["item_id"])
        if db_item_id is None:
            continue
        if db_item_id in existing_decisions:
            # Idempotent resume/finalisation: the row was already checkpointed by its
            # tier. Only the cluster membership (computed at the end of the graph)
            # and the reviewer's final verdict are back-filled onto it.
            row = existing_decisions[db_item_id]
            cluster_id = cluster_of_item.get(decision["item_id"])
            if cluster_id and not getattr(row, "cluster_id", None):
                row.cluster_id = cluster_id
            _assign(
                row,
                decision_cls,
                {
                    "proposed_action": decision["proposed_action"],
                    "confidence": float(decision["confidence"]),
                    "reasoning": decision["reasoning"],
                    "decided_by": decision["decided_by"],
                    "status": decision.get("status", "proposed"),
                },
            )
            if decision.get("review_state"):
                row.review_state = decision["review_state"]
            continue
        row = _new(
            decision_cls,
            {
                "user_id": user_id,
                "run_id": run_id,
                "item_id": db_item_id,
                "cluster_id": cluster_of_item.get(decision["item_id"]),
                "category_id": category_ids.get(decision.get("category") or ""),
                "proposed_action": decision["proposed_action"],
                "confidence": float(decision["confidence"]),
                "reasoning": decision["reasoning"],
                "decided_by": decision["decided_by"],
                "rule_id": decision.get("rule_id"),
                "time_sensitive": bool(decision.get("time_sensitive")),
                "status": decision.get("status", "proposed"),
                "review_state": decision.get("review_state") or "provisional",
            },
        )
        session.add(row)
        existing_decisions[db_item_id] = row
        written += 1

    call_cls = model_for("llm_calls")
    if call_cls is not None:
        for call in llm_calls:
            # Calls already written by graph.checkpoint.record_batch carry this
            # marker — writing them again would double-count the run's spend.
            if call.get("_checkpointed"):
                continue
            session.add(
                _new(
                    call_cls,
                    {
                        "user_id": user_id,
                        "run_id": run_id,
                        "purpose": call.get("purpose", "classify"),
                        "model": call.get("model", ""),
                        "items_in_batch": call.get("items_in_batch", 0),
                        "tokens_in": call.get("tokens_in", 0),
                        "tokens_out": call.get("tokens_out", 0),
                        "cost_usd": call.get("cost_usd", 0.0),
                        "latency_ms": call.get("latency_ms", 0),
                    },
                )
            )

    session.flush()
    # Cost is summed from the persisted llm_calls rows, never from this leg's
    # in-memory list: a resumed run's state only holds the calls it made itself, so
    # taking the in-memory total would reset the run's spend instead of adding to it.
    update_run(
        session,
        run_id=run_id,
        status=status,
        items_total=max(len(items), len(existing_decisions)),
        items_decided=len(existing_decisions),
        counts=counts,
        cost=run_cost_totals(session, run_id),
        error=error,
    )
    session.flush()
    return {"decisions_written": written, "clusters_written": len(clusters)}


def update_run(
    session: Session,
    *,
    run_id: str,
    status: str | None = None,
    items_total: int | None = None,
    items_decided: int | None = None,
    counts: dict | None = None,
    cost: dict | None = None,
    error: str | None = None,
) -> None:
    run_cls = model_for("triage_runs")
    if run_cls is None:
        return
    row = session.get(run_cls, run_id)
    if row is None:
        return
    # `cancelled` is terminal: the user's cancel (written by the API) must never
    # be overwritten by a graph still draining — not by "running", "completed"
    # or anything else. Counts/cost are still recorded for the audit trail.
    current_status = getattr(row, "status", None)
    values: dict[str, Any] = {}
    if status is not None and current_status != "cancelled":
        values["status"] = status
    if items_total is not None:
        # Never shrink the denominator: a resumed run only fetches the threads it
        # still has to decide, so its leg-local total is smaller than the mailbox.
        values["items_total"] = max(int(items_total), int(getattr(row, "items_total", 0) or 0))
    if items_decided is not None:
        values["items_decided"] = items_decided
    if counts is not None:
        values["counts"] = counts
    if cost is not None:
        values["tokens_in"] = cost.get("tokens_in", 0)
        values["tokens_out"] = cost.get("tokens_out", 0)
        values["cost_usd"] = cost.get("usd", 0.0)
    if error is not None:
        values["error_message"] = error
    # `resumable` is NOT terminal — the run can be put back to `running` by
    # POST /api/runs/{id}/resume — so it never gets a finished_at stamp.
    if status in ("completed", "failed", "cancelled") and current_status != "cancelled":
        from datetime import datetime, timezone

        values["finished_at"] = datetime.now(timezone.utc)
    _assign(row, run_cls, values)
