"""Nodes of the cost-tiered triage cascade. See spec/agent.md.

Tier 1 (deterministic rules) and tier 2 (sender history) are free and resolve the
obvious majority. Only the remainder reaches the LLM, batched 20-50 threads per call.
Borderline threads escalate to a single-item deep read. Everything below the confidence
floor becomes ``needs_your_call`` — the agent never archives what it is unsure about.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

from graph.persistence import DEFAULT_CONFIDENCE_FLOOR, load_context as _load_context_rows
from graph.state import TriageState
from observability.events import get_logger
from tools import clustering, rules as rules_tool
from tools.redact import redact, redact_items as _redact_all

log = get_logger("triage")

_PROMPT_DIR = Path(__file__).parent.parent / "prompts"

MIN_BATCH = 20
MAX_BATCH = 50
TARGET_BATCH = 30
MAX_DEEP_READS = 25
UNSURE_MAX_CONFIDENCE = 0.5
VALID_ACTIONS = set(rules_tool.VALID_ACTIONS)


# --------------------------------------------------------------------- helpers


def _prompt(name: str) -> str:
    return (_PROMPT_DIR / name).read_text(encoding="utf-8")


def _category_block(categories: list[dict]) -> str:
    return "\n".join(
        f"- `{c['key']}` ({c.get('name', c['key'])}): {c.get('description', '')}"
        for c in categories
    )


def _priorities_block(state: TriageState) -> str:
    """The user's priorities profile, injected verbatim — never rewritten by the
    agent. Empty when the user has not written one yet."""
    text = (state.get("priorities_profile") or "").strip()
    return text or "(The user has not written a priorities profile yet.)"


def _age_days(item: dict) -> int | None:
    raw = item.get("internal_date")
    if isinstance(raw, str):
        try:
            raw = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if not isinstance(raw, datetime):
        return None
    if raw.tzinfo is None:
        raw = raw.replace(tzinfo=timezone.utc)
    return int((datetime.now(timezone.utc) - raw).total_seconds() // 86400)


def _thread_payload(item: dict) -> dict:
    """The ONLY shape that ever leaves the machine: headers + subject + redacted snippet."""
    return {
        "item_id": item["id"],
        "from": item.get("from_name") or "",
        "from_email": item.get("from_email") or "",
        "domain": item.get("from_domain") or "",
        "list_id": item.get("list_id") or None,
        "has_unsubscribe": bool(item.get("unsubscribe_url")),
        "subject": item.get("subject") or "",
        "snippet": item.get("snippet_redacted") or "",
        "messages": item.get("message_count", 1),
        "unread": bool(item.get("is_unread")),
        "attachments": bool(item.get("has_attachments")),
        "age_days": _age_days(item),
    }


def _thread_block(items: list[dict]) -> str:
    return "\n".join(json.dumps(_thread_payload(i), ensure_ascii=False) for i in items)


def _verdict_schema(category_keys: list[str]) -> dict:
    return {
        "type": "object",
        "required": ["item_id", "category", "action", "confidence", "reasoning"],
        "properties": {
            "item_id": {"type": "string"},
            "category": {"type": "string", "enum": sorted(category_keys)},
            "action": {"type": "string", "enum": sorted(VALID_ACTIONS)},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reasoning": {"type": "string", "minLength": 1},
            "time_sensitive": {"type": "boolean"},
            "unsure": {"type": "boolean"},
        },
        "additionalProperties": True,
    }


_LLM_TIMEOUT = 120.0  # seconds; a hung LLM call raises asyncio.TimeoutError


def _run_async(coro, timeout: float = _LLM_TIMEOUT):
    """Run an async LLM coroutine from a synchronous graph node.

    Always applies a hard timeout so a hung NIM/API connection raises
    asyncio.TimeoutError (caught by callers' except Exception) rather than
    blocking forever and stalling the whole triage run.
    """
    import asyncio

    wrapped = asyncio.wait_for(coro, timeout=timeout)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(wrapped)

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, wrapped).result(timeout=timeout + 5)


def _usage_row(usage, purpose: str, items_in_batch: int) -> dict:
    return {
        "purpose": purpose,
        "items_in_batch": items_in_batch,
        "model": getattr(usage, "model", "") or "",
        "tokens_in": int(getattr(usage, "tokens_in", 0) or 0),
        "tokens_out": int(getattr(usage, "tokens_out", 0) or 0),
        "cost_usd": float(getattr(usage, "usd", 0.0) or 0.0),
        "latency_ms": int(getattr(usage, "latency_ms", 0) or 0),
    }


def _model_candidates(state: TriageState) -> list[str | None]:
    """The user's preferred model first, then the provider default as a fallback.

    A stale or retired per-user model id must never take a whole run down with it.
    """
    preferred = (state.get("settings") or {}).get("llm_model")
    return [preferred, None] if preferred else [None]


def _call_llm(
    prompt: str,
    *,
    model: str | None = None,
    json_schema: dict | None = None,
    max_tokens: int = 4096,
) -> tuple[str, list[dict]]:
    """Single completion through the shared client. Returns ``(text, usage_rows)``.

    JSON calls get the schema as a real ``response_format`` with thinking disabled
    (reasoning models otherwise burn the budget on chain-of-thought). A reply cut
    off at the token limit is never parsed — it is re-issued once with double the
    budget. Every call's spend is returned, parse outcome notwithstanding.
    """
    from llm.client import get_llm_client

    client = get_llm_client()
    rows: list[dict] = []
    result = _run_async(
        client.call_model(
            prompt,
            model=model,
            json_schema=json_schema,
            disable_thinking=json_schema is not None,
            max_tokens=max_tokens,
        )
    )
    rows.append(_usage_row(result, "deep_read", 1))
    if json_schema is not None and getattr(result, "finish_reason", None) == "length":
        result = _run_async(
            client.call_model(
                prompt,
                model=model,
                json_schema=json_schema,
                disable_thinking=True,
                max_tokens=max_tokens * 2,
            )
        )
        rows.append(_usage_row(result, "deep_read", 1))
    return result.text, rows


def _run_is_cancelled(run_id: str | None) -> bool:
    """True iff the user cancelled this run (persisted flag on the run row)."""
    if not run_id:
        return False
    try:
        from db.session import create_db_session
        from graph.persistence import model_for

        run_cls = model_for("triage_runs")
        if run_cls is None:
            return False
        with create_db_session() as session:
            row = session.get(run_cls, run_id)
            return row is not None and getattr(row, "status", None) == "cancelled"
    except Exception as exc:  # pragma: no cover - a broken check must not kill a run
        log.warning("triage.cancel_check_failed", run_id=run_id, error=str(exc))
        return False


def _record_progress(
    run_id: str | None,
    *,
    items_total: int | None = None,
    items_decided: int | None = None,
    decided_delta: int | None = None,
) -> None:
    """Short-transaction progress write so the 1s poll sees live numbers.

    ``decided_delta`` uses an atomic column-expression UPDATE — parallel batch
    branches may increment concurrently.
    """
    if not run_id:
        return
    try:
        from sqlalchemy import update as sa_update

        from db.session import create_db_session
        from graph.persistence import model_for

        run_cls = model_for("triage_runs")
        if run_cls is None:
            return
        with create_db_session() as session:
            values: dict = {}
            if items_total is not None:
                values["items_total"] = items_total
            if items_decided is not None:
                values["items_decided"] = items_decided
            if values:
                session.execute(
                    sa_update(run_cls).where(run_cls.id == run_id).values(**values)
                )
            if decided_delta:
                session.execute(
                    sa_update(run_cls)
                    .where(run_cls.id == run_id)
                    .values(items_decided=run_cls.items_decided + decided_delta)
                )
    except Exception as exc:  # pragma: no cover - progress must never fail a run
        log.warning("triage.progress_write_failed", run_id=run_id, error=str(exc))


_FENCE_RE = re.compile(r"```(?:json)?", re.IGNORECASE)


def _extract_json(text: str, *, array: bool) -> object:
    cleaned = _FENCE_RE.sub("", text or "").strip()
    open_ch, close_ch = ("[", "]") if array else ("{", "}")
    start = cleaned.find(open_ch)
    end = cleaned.rfind(close_ch)
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON payload in LLM response")
    return json.loads(cleaned[start : end + 1])


def _normalise_verdict(raw: dict, item: dict, category_keys: set[str]) -> dict:
    category = str(raw.get("category") or "").strip().lower()
    if category not in category_keys:
        category = "people" if "people" in category_keys else sorted(category_keys)[0]
    action = str(raw.get("action") or "keep").strip().lower()
    if action not in VALID_ACTIONS:
        action = "keep"
    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    reasoning = str(raw.get("reasoning") or "").strip()
    if not reasoning:
        reasoning = (
            f"Classified as {category} from the sender {item.get('from_email')!r} "
            "and the subject line."
        )
    time_sensitive = bool(raw.get("time_sensitive"))
    unsure = bool(raw.get("unsure"))

    if time_sensitive:
        action = "keep"
    if unsure:
        action = "keep"
        confidence = min(confidence, UNSURE_MAX_CONFIDENCE)

    return {
        "item_id": item["id"],
        "category": category,
        "proposed_action": action,
        "confidence": confidence,
        "reasoning": redact(reasoning),
        "decided_by": "llm",
        "rule_id": None,
        "time_sensitive": time_sensitive,
        "unsure": unsure,
    }


def _degraded(items: list[dict], reason: str) -> list[dict]:
    """A failed batch keeps every thread visible — never archives."""
    return [
        {
            "item_id": item["id"],
            "category": None,
            "proposed_action": "keep",
            "confidence": 0.0,
            "reasoning": (
                f"Automatic classification failed ({reason}), so this thread was kept "
                "in your inbox and routed to you for a decision."
            ),
            "decided_by": "error",
            "rule_id": None,
            "time_sensitive": False,
            "unsure": False,
        }
        for item in items
    ]


def apply_confidence_floor(decisions: list[dict], floor: float) -> list[dict]:
    """Below the floor the agent never archives: force keep + ``needs_your_call``."""
    out = []
    for decision in decisions:
        decision = dict(decision)
        if float(decision.get("confidence") or 0.0) < floor:
            if decision.get("proposed_action") != "keep":
                decision["reasoning"] = (
                    f"{decision['reasoning']} Confidence "
                    f"{decision.get('confidence'):.2f} is below the {floor:.2f} floor, "
                    "so nothing is proposed and this is left for you to decide."
                )
            decision["proposed_action"] = "keep"
            decision["status"] = "needs_your_call"
        else:
            decision.setdefault("status", "proposed")
        out.append(decision)
    return out


def _chunk(items: list[dict]) -> list[list[dict]]:
    """Chunk into batches of 20-50 (target 30) by count/token budget."""
    if not items:
        return []
    if len(items) <= MAX_BATCH:
        return [items]
    count = max(1, min(math.ceil(len(items) / TARGET_BATCH), len(items) // MIN_BATCH))
    base, remainder = divmod(len(items), count)
    batches: list[list[dict]] = []
    cursor = 0
    for index in range(count):
        size = base + (1 if index < remainder else 0)
        batches.append(items[cursor : cursor + size])
        cursor += size
    return batches


# ----------------------------------------------------------------------- nodes


def load_context(state: TriageState) -> dict:
    try:
        from db.session import create_db_session

        with create_db_session() as session:
            context = _load_context_rows(session, state["user_id"])
        log.info(
            "triage.context_loaded",
            run_id=state.get("run_id"),
            categories=len(context["categories"]),
            rules=len(context["rules"]),
            senders=len(context["sender_stats"]),
        )
        return {**context, "error": None}
    except Exception as exc:  # pragma: no cover - defensive
        return {"error": f"load_context failed: {exc}"}


def fetch_items(state: TriageState) -> dict:
    """Normalised items, headers + subject + <=200 char snippet only."""
    if state.get("items"):
        items = list(state["items"])
        _record_progress(state.get("run_id"), items_total=len(items))
        return {"items": items, "error": None}
    try:
        from channels import get_adapter

        adapter = get_adapter(
            user_id=state["user_id"], channel_account_id=state["channel_account_id"]
        )
        run_id = state.get("run_id")
        user_id = state["user_id"]
        fetch_after = state.get("fetch_after")
        after_dt = None
        if fetch_after:
            from datetime import datetime, timezone

            after_dt = datetime.fromisoformat(fetch_after)
            # TriageRun.started_at round-trips through SQLite naive (no tzinfo),
            # while ChannelItem.internal_date is always UTC-aware (Gmail's
            # internalDate is parsed with tz=timezone.utc) — comparing the two
            # naively raises TypeError. SQLite never stored anything but UTC
            # here (see db/session.py / _now()), so naive-means-UTC is correct.
            if after_dt.tzinfo is None:
                after_dt = after_dt.replace(tzinfo=timezone.utc)

        def _on_fetch_page(page_num: int, fetched_so_far: int) -> None:
            try:
                from events import bus as _bus
                _bus.emit(user_id, {
                    "type": "fetch_progress",
                    "run_id": run_id,
                    "page": page_num,
                    "fetched_so_far": fetched_so_far,
                })
            except Exception:  # pragma: no cover - bus must never fail a run
                pass

        items = [
            i if isinstance(i, dict) else i.model_dump()
            for i in adapter.list_threads(
                limit=state.get("limit", 200),
                cancel_check=(lambda: _run_is_cancelled(run_id)) if run_id else None,
                after=after_dt,
                on_page=_on_fetch_page,
            )
        ]
        # The progress bar's denominator, written the moment the total is known —
        # not at the end of the run (spec/api.md: GET /api/runs is polled at 1s).
        _record_progress(state.get("run_id"), items_total=len(items))
        # Harvest + persist per-sender evidence so the never-miss reply-history
        # signal (sender_profiles.ever_replied) and the learned bulk-archive
        # heuristic feed tier 2 of the cascade. Best-effort: a channel read
        # failure must not fail ingestion of the threads already fetched.
        sender_stats = _harvest_sender_stats(adapter, state["user_id"])
        # Un-archive correction detection (spec/capabilities/user-memory.md): if a
        # thread we previously archived (an `applied` archive Decision) is fetched
        # again with INBOX present, the user manually restored it — that is the
        # strongest correction signal and must raise the sender's importance
        # score. Best-effort: never fails ingestion of the threads already fetched.
        _detect_and_record_mailbox_corrections(state["user_id"], items)
        return {
            "items": items,
            "sender_stats": sender_stats,
            "error": None,
        }
    except Exception as exc:
        return {"error": f"fetch_items failed: {exc}"}


def _detect_and_record_mailbox_corrections(user_id: str, items: list[dict]) -> None:
    """Detect threads the agent archived that are now back in INBOX.

    Reconciliation, not the app's own undo flow: a decision the user reversed via
    ``POST /api/actions/{id}/undo`` is marked ``status="undone"`` by
    ``tools.actions.undo_action`` and is skipped here — only an ``applied`` archive
    decision whose thread has INBOX again (i.e. reversed *outside* the app, by the
    user re-adding it in Gmail) counts as a correction.
    """
    from channels.gmail.mutations import INBOX_LABEL_ID
    from db.session import create_db_session
    from tools.memory import record_correction

    restored_thread_ids = [
        item.get("external_thread_id")
        for item in items
        if item.get("external_thread_id") and INBOX_LABEL_ID in (item.get("channel_labels") or [])
    ]
    if not restored_thread_ids:
        return
    try:
        with create_db_session() as session:
            from db import models as m
            from sqlalchemy import select

            restored_items = list(
                session.execute(
                    select(m.Item).where(
                        m.Item.user_id == user_id,
                        m.Item.external_thread_id.in_(restored_thread_ids),
                    )
                ).scalars()
            )
            for db_item in restored_items:
                decision = session.execute(
                    select(m.Decision)
                    .where(
                        m.Decision.item_id == db_item.id,
                        m.Decision.user_id == user_id,
                        m.Decision.status == "applied",
                        m.Decision.proposed_action == "archive",
                    )
                    .order_by(m.Decision.decided_at.desc(), m.Decision.created_at.desc())
                ).scalars().first()
                if decision is None:
                    continue
                already_recorded = session.execute(
                    select(m.Correction.id).where(
                        m.Correction.user_id == user_id,
                        m.Correction.decision_id == decision.id,
                        m.Correction.source == "mailbox_reconciliation",
                    )
                ).scalar_one_or_none()
                if already_recorded is not None:
                    continue
                record_correction(
                    session,
                    user_id,
                    item_id=db_item.id,
                    from_action="archive",
                    to_action="keep",
                    source="mailbox_reconciliation",
                    decision_id=decision.id,
                    note="thread found back in INBOX outside the app's own undo flow",
                )
            session.flush()
    except Exception as exc:  # pragma: no cover - best effort, never blocks a run
        log.warning("triage.mailbox_correction_detect_failed", user_id=user_id, error=str(exc))


def _harvest_sender_stats(adapter, user_id: str) -> dict[str, dict]:
    """Pull sender evidence from the channel and persist it for this user.

    Returns the in-memory ``sender_stats`` map (keyed by lowercased sender
    email) so downstream nodes can use it without a re-read; the rows are also
    written to ``sender_profiles`` per spec/data.md + spec/capabilities/thread-ingestion.md
    ("Sender evidence updates → sender_profiles").
    """
    from db.session import create_db_session
    from graph.persistence import persist_sender_profiles

    signals = adapter.sender_history()
    # ``sender_history`` returns ``SenderSignal`` per address the user has
    # replied to (harvested from the SENT label). It carries reply evidence only;
    # the received/opened/archived counts are maintained from the inbox pass
    # elsewhere and are merged here as 0 (a sender not in the sent folder has 0
    # reply-derived evidence yet is still tracked so it can be learned later).
    stats = {
        email: {
            "received_count": 0,
            "opened_count": 0,
            "replied_count": sig.get("replied_count", 0) if hasattr(sig, "get") else getattr(sig, "replied_count", 0),
            "archived_by_user_count": 0,
            "ever_replied": bool(sig.get("ever_replied", False) if hasattr(sig, "get") else getattr(sig, "ever_replied", False)),
            "last_replied_at": sig.get("last_replied_at") if hasattr(sig, "get") else getattr(sig, "last_replied_at", None),
            "last_seen_at": datetime.now(timezone.utc),
        }
        for email, sig in (signals or {}).items()
    }
    try:
        with create_db_session() as session:
            persist_sender_profiles(session, user_id=user_id, signals=stats)
    except Exception as exc:  # pragma: no cover - best effort, never blocks a run
        log.warning("triage.sender_profile_persist_failed", user_id=user_id, error=str(exc))
    return stats


def redact_items(state: TriageState) -> dict:
    """The single egress chokepoint — nothing reaches an LLM unredacted."""
    items = _redact_all(list(state.get("items") or []))
    return {"items": items}


def apply_deterministic_rules(state: TriageState) -> dict:
    decisions, unresolved = rules_tool.apply_rules(
        state.get("items") or [], state.get("rules") or []
    )
    log.info(
        "triage.tier1", run_id=state.get("run_id"), resolved=len(decisions),
        remaining=len(unresolved),
    )
    return {"resolved": decisions, "llm_queue": unresolved}


def apply_sender_history(state: TriageState) -> dict:
    decisions, unresolved = rules_tool.apply_sender_history(
        state.get("llm_queue") or [], state.get("sender_stats") or {}
    )
    log.info(
        "triage.tier2", run_id=state.get("run_id"), resolved=len(decisions),
        remaining=len(unresolved),
    )
    return {"resolved": decisions, "llm_queue": unresolved}


def prepare_llm_batches(state: TriageState) -> dict:
    batches = _chunk(state.get("llm_queue") or [])
    # Tiers 1-2 are done by now: surface their progress before the LLM starts.
    _record_progress(
        state.get("run_id"), items_decided=len(state.get("resolved") or [])
    )
    log.info(
        "triage.batches", run_id=state.get("run_id"), batches=len(batches),
        items=len(state.get("llm_queue") or []),
    )
    return {"batches": batches}


def llm_classify_batch(state: TriageState) -> dict:
    """Tier 3 — one LLM call classifies a whole batch of 20-50 threads."""
    batch = list(state.get("batch") or [])
    if not batch:
        return {}

    run_id = state.get("run_id")
    if _run_is_cancelled(run_id):
        # The cancel flag is persisted by POST /api/runs/{id}/cancel; honour it
        # between batches by spending no further tokens. A cancelled run must
        # never synthesize decisions for undecided threads — those rows would
        # persist as decided_by="error" and bury the user's real results. This
        # batch is simply dropped: no decision, no cluster, nothing archived.
        log.info("triage.batch_skipped_cancelled", run_id=run_id, batch_size=len(batch))
        return {
            "llm_decisions": [],
            "deep_queue": [],
            "llm_calls": [],
        }

    categories = state.get("categories") or []
    category_keys = {c["key"] for c in categories}
    by_id = {i["id"]: i for i in batch}
    instructions = (
        _prompt("classify.md")
        .replace("{categories}", _category_block(categories))
        .replace("{priorities}", _priorities_block(state))
    )
    from llm.client import get_llm_client

    result = None
    last_error = "unknown error"
    failed_calls: list[dict] = []
    for model in _model_candidates(state):
        try:
            result = _run_async(
                get_llm_client().classify_batch(
                    [_thread_payload(item) for item in batch],
                    instructions=instructions,
                    item_schema=_verdict_schema(sorted(category_keys)),
                    id_field="item_id",
                    model=model,
                    max_attempts=2,
                )
            )
            break
        except Exception as exc:
            last_error = str(exc)
            # Tokens burnt by a failed batch are still real spend — keep them for
            # the audit trail (spec/capabilities/decision-audit-trail.md).
            failed_usage = getattr(exc, "usage", None)
            if failed_usage is not None:
                failed_calls.append(_usage_row(failed_usage, "classify_failed", len(batch)))
            log.warning(
                "triage.tier3_model_failed",
                run_id=state.get("run_id"),
                model=model,
                error=last_error,
            )

    if result is None:
        log.error("triage.tier3_failed", run_id=state.get("run_id"), error=last_error)
        return {
            "llm_decisions": _degraded(batch, last_error),
            "deep_queue": [],
            "llm_calls": failed_calls,
        }

    decisions: list[dict] = []
    verdicts = result.by_id
    for item in batch:
        raw = verdicts.get(item["id"])
        if raw is None:
            decisions.append(
                _degraded([item], "the model returned no valid verdict for this thread")[0]
            )
        else:
            decisions.append(_normalise_verdict(raw, item, category_keys))

    deep = [by_id[d["item_id"]] for d in decisions if d.get("unsure")]
    calls = failed_calls + (
        [_usage_row(result.usage, "classify", len(batch))] if result.usage else []
    )
    # Live progress: this batch is decided — bump the numerator atomically.
    _record_progress(run_id, decided_delta=len(batch))
    # Emit an SSE progress event so the frontend sees incremental LLM progress
    # rather than waiting until persist_decisions fires at the very end.
    try:
        from events import bus as _bus

        _bus.emit(
            state["user_id"],
            {
                "type": "run_progress",
                "run_id": run_id,
                "items_decided": len(state.get("llm_decisions", [])) + len(state.get("resolved", [])),
                "cost_so_far": sum(float(c.get("cost_usd") or 0.0) for c in (state.get("llm_calls") or []) + calls),
            },
        )
    except Exception:  # pragma: no cover - bus must never fail a run
        pass
    log.info(
        "triage.tier3",
        run_id=state.get("run_id"),
        batch_size=len(batch),
        verdicts=len(result.results),
        missing=len(result.missing_ids),
        invalid=len(result.invalid),
        unsure=len(deep),
    )
    return {"llm_decisions": decisions, "deep_queue": deep, "llm_calls": calls}


def deep_read_escalation(state: TriageState) -> dict:
    """Tier 4 — one call per borderline thread, capped at 25 per run.

    Overflow keeps its low-confidence batch verdict, which lands below the floor and
    therefore in ``needs_your_call``. Never archived.
    """
    queue = list(state.get("deep_queue") or [])[:MAX_DEEP_READS]
    if not queue:
        return {}

    run_id = state.get("run_id")
    categories = state.get("categories") or []
    category_keys = {c["key"] for c in categories}
    schema = _verdict_schema(sorted(category_keys))
    template = _prompt("deep_read.md")
    candidates = _model_candidates(state)
    sender_stats = state.get("sender_stats") or {}

    decisions: list[dict] = []
    calls: list[dict] = []
    for item in queue:
        if _run_is_cancelled(run_id):
            # Remaining deep reads are skipped; each keeps its low-confidence batch
            # verdict, which lands below the floor -> needs_your_call. Never archived.
            log.info(
                "triage.deep_reads_skipped_cancelled",
                run_id=run_id,
                remaining=len(queue) - len(decisions),
            )
            break
        stats = sender_stats.get((item.get("from_email") or "").lower()) or {}
        history = (
            json.dumps(
                {
                    "sender": item.get("from_email"),
                    "received": stats.get("received_count", 0),
                    "replied": stats.get("replied_count", 0),
                    "ever_replied": bool(stats.get("ever_replied")),
                    "archived_by_user": stats.get("archived_by_user_count", 0),
                }
            )
            if stats
            else "No prior correspondence recorded with this sender."
        )
        prompt = (
            template.replace("{categories}", _category_block(categories))
            .replace("{sender_history}", history)
            .replace("{thread}", _thread_block([item]))
        )
        for model in candidates:
            try:
                text, metas = _call_llm(prompt, model=model, json_schema=schema)
                # Spend is recorded even if the parse below fails — the tokens are real.
                calls.extend(metas)
                raw = _extract_json(text, array=False)
                if not isinstance(raw, dict):
                    raise ValueError("expected a JSON object")
                verdict = _normalise_verdict(raw, item, category_keys)
                verdict["decided_by"] = "llm_deep"
                verdict["unsure"] = False
                decisions.append(verdict)
                # Deep reads land one item at a time (unlike a batch, which lands
                # all at once) — the progress bar's numerator advances with each
                # one instead of waiting for the whole queue to finish.
                _record_progress(run_id, decided_delta=1)
                break
            except Exception as exc:
                # The batch verdict stands: low confidence -> needs_your_call, never archive.
                log.warning(
                    "triage.tier4_failed", run_id=state.get("run_id"), item_id=item["id"],
                    model=model, error=str(exc),
                )

    log.info("triage.tier4", run_id=state.get("run_id"), deep_reads=len(decisions))
    return {"llm_decisions": decisions, "llm_calls": calls}


_TIER_PRECEDENCE = {"error": 0, "rule": 1, "sender_history": 1, "llm": 2, "llm_deep": 3}


def _merge_decisions(state: TriageState, *, cancelled: bool = False) -> list[dict]:
    """One decision per item; the deepest tier wins. Undecided items degrade to keep.

    A cancelled run is the one exception: items with no decision yet are left
    out entirely rather than synthesized as degraded ``decided_by="error"``
    rows, so cancelling never persists placeholder decisions that bury the
    user's real, already-decided threads.
    """
    best: dict[str, dict] = {}
    for decision in list(state.get("resolved") or []) + list(state.get("llm_decisions") or []):
        current = best.get(decision["item_id"])
        if current is None or _TIER_PRECEDENCE.get(
            decision["decided_by"], 0
        ) >= _TIER_PRECEDENCE.get(current["decided_by"], 0):
            best[decision["item_id"]] = decision

    if not cancelled:
        for item in state.get("items") or []:
            if item["id"] not in best:
                best[item["id"]] = _degraded([item], "no verdict was produced")[0]

    order = {item["id"]: n for n, item in enumerate(state.get("items") or [])}
    return sorted(best.values(), key=lambda d: order.get(d["item_id"], 0))


def cluster_decisions(state: TriageState) -> dict:
    try:
        floor = float(
            (state.get("settings") or {}).get("confidence_floor", DEFAULT_CONFIDENCE_FLOOR)
        )
        cancelled = _run_is_cancelled(state.get("run_id"))
        decisions = apply_confidence_floor(_merge_decisions(state, cancelled=cancelled), floor)
        clusters = clustering.cluster(
            decisions, state.get("items") or [], categories=state.get("categories") or []
        )
        log.info(
            "triage.clustered", run_id=state.get("run_id"), decisions=len(decisions),
            clusters=len(clusters),
        )
        return {"decisions": decisions, "clusters": clusters, "error": None}
    except Exception as exc:
        return {"error": f"cluster_decisions failed: {exc}"}


def _counts(decisions: list[dict]) -> dict:
    by_tier: dict[str, int] = {}
    by_category: dict[str, int] = {}
    by_action: dict[str, int] = {}
    needs_your_call = 0
    for decision in decisions:
        by_tier[decision["decided_by"]] = by_tier.get(decision["decided_by"], 0) + 1
        key = decision.get("category") or "uncategorised"
        by_category[key] = by_category.get(key, 0) + 1
        by_action[decision["proposed_action"]] = by_action.get(decision["proposed_action"], 0) + 1
        if decision.get("status") == "needs_your_call":
            needs_your_call += 1
    return {
        "total": len(decisions),
        "by_tier": by_tier,
        "by_category": by_category,
        "by_action": by_action,
        "needs_your_call": needs_your_call,
    }


def _cost(state: TriageState) -> dict:
    calls = state.get("llm_calls") or []
    return {
        "tokens_in": sum(int(c.get("tokens_in") or 0) for c in calls),
        "tokens_out": sum(int(c.get("tokens_out") or 0) for c in calls),
        "usd": round(sum(float(c.get("cost_usd") or 0.0) for c in calls), 6),
        "llm_calls": len(calls),
    }


def persist_decisions(state: TriageState) -> dict:
    try:
        from db.session import create_db_session
        from graph.persistence import persist_run_results

        floor = float(
            (state.get("settings") or {}).get("confidence_floor", DEFAULT_CONFIDENCE_FLOOR)
        )
        decisions = apply_confidence_floor(state.get("decisions") or [], floor)
        counts = _counts(decisions)
        cost = _cost(state)

        with create_db_session() as session:
            persist_run_results(
                session,
                run_id=state["run_id"],
                user_id=state["user_id"],
                channel_account_id=state.get("channel_account_id") or "",
                items=state.get("items") or [],
                decisions=decisions,
                clusters=state.get("clusters") or [],
                counts=counts,
                cost=cost,
                llm_calls=state.get("llm_calls") or [],
                status="running",
            )
        try:
            from events import bus

            bus.emit(
                state["user_id"],
                {
                    "type": "run_progress",
                    "ts": __import__("time").time(),
                    "run_id": state["run_id"],
                    "items_decided": counts.get("total", 0),
                    "cost_so_far": cost.get("usd", 0.0),
                },
            )
        except Exception:  # pragma: no cover - event bus must never fail a run
            pass

        return {"decisions": decisions, "counts": counts, "cost": cost, "error": None}
    except Exception as exc:
        return {"error": f"persist_decisions failed: {exc}"}


def handle_error(state: TriageState) -> dict:
    """Degradation always keeps mail visible."""
    error = state.get("error") or "unknown error"
    decisions = apply_confidence_floor(_merge_decisions(state), 1.1)  # everything to the user
    counts = _counts(decisions)
    try:
        from db.session import create_db_session
        from graph.persistence import persist_run_results

        with create_db_session() as session:
            persist_run_results(
                session,
                run_id=state["run_id"],
                user_id=state["user_id"],
                channel_account_id=state.get("channel_account_id") or "",
                items=state.get("items") or [],
                decisions=decisions,
                clusters=[],
                counts=counts,
                cost=_cost(state),
                llm_calls=state.get("llm_calls") or [],
                status="failed",
                error=error,
            )
    except Exception as exc:  # pragma: no cover - the run is already failing
        log.error("triage.persist_on_error_failed", error=str(exc))
    log.error("triage.failed", run_id=state.get("run_id"), error=error)

    try:
        from events import bus

        bus.emit(
            state["user_id"],
            {
                "type": "error",
                "ts": __import__("time").time(),
                "run_id": state.get("run_id"),
                "message": error,
            },
        )
    except Exception:  # pragma: no cover
        pass

    return {"status": "failed", "decisions": decisions, "counts": counts}


def _build_mutator_for_user(user_id: str, channel_account_id: str, session):
    """Build a GmailMutator + GmailLabelManager for auto-apply.

    Returns ``(mutator, label_lookup)`` or raises on auth failure.
    """
    from googleapiclient.discovery import build

    from channels.gmail.labels import GmailLabelManager
    from channels.gmail.mutations import GmailMutator
    from channels.gmail.oauth import credentials_from_refresh_token, google_oauth_config
    from channels.gmail.store import SqlConnectionStore

    refresh_token = SqlConnectionStore().load_refresh_token(
        user_id=user_id, connection_id=channel_account_id
    )
    config = google_oauth_config()
    credentials = credentials_from_refresh_token(config, refresh_token)
    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    return GmailMutator(service), GmailLabelManager(service)


def _auto_apply_decisions(state: TriageState, counts: dict) -> None:
    """Apply all non-keep, non-needs_your_call decisions for this run.

    Called from ``finalize`` when dry_run is off. Each decision is applied
    independently — one failure never blocks the others.
    """
    run_id = state.get("run_id")
    user_id = state["user_id"]
    channel_account_id = state.get("channel_account_id") or ""

    applied = 0
    kept = 0
    auto_kept_low_confidence = 0
    errors: list[str] = []

    try:
        from db.session import create_db_session
        from db.models import Decision as DecisionModel
        from sqlalchemy import select as sa_select
        from tools.actions import apply_decision, ActionsError, NeedsYourCallError, NotApprovedError
        from channels.base import ChannelError, DryRunViolation

        with create_db_session() as session:
            try:
                mutator, label_lookup = _build_mutator_for_user(user_id, channel_account_id, session)
            except Exception as exc:
                log.error(
                    "triage.auto_apply_build_mutator_failed",
                    run_id=run_id,
                    error=str(exc),
                )
                return

            decisions = list(
                session.execute(
                    sa_select(DecisionModel).where(
                        DecisionModel.run_id == run_id,
                        DecisionModel.user_id == user_id,
                    )
                ).scalars()
            )

            for decision in decisions:
                if decision.status == "needs_your_call":
                    auto_kept_low_confidence += 1
                    continue
                if decision.proposed_action not in ("archive", "digest"):
                    kept += 1
                    continue
                # Mark approved so apply_decision accepts it.
                decision.status = "approved"
                session.flush()
                try:
                    apply_decision(
                        session,
                        user_id,
                        decision.id,
                        mutator=mutator,
                        label_lookup=label_lookup,
                        dry_run=False,
                    )
                    session.commit()
                    applied += 1
                except (ActionsError, NeedsYourCallError, NotApprovedError) as exc:
                    session.rollback()
                    errors.append(str(exc))
                    log.warning(
                        "triage.auto_apply_decision_failed",
                        run_id=run_id,
                        decision_id=decision.id,
                        error=str(exc),
                    )
                except (ChannelError, DryRunViolation) as exc:
                    session.rollback()
                    errors.append(str(exc))
                    log.warning(
                        "triage.auto_apply_gmail_failed",
                        run_id=run_id,
                        decision_id=decision.id,
                        error=str(exc),
                    )
                except Exception as exc:
                    session.rollback()
                    errors.append(str(exc))
                    log.warning(
                        "triage.auto_apply_unexpected_error",
                        run_id=run_id,
                        decision_id=decision.id,
                        error=str(exc),
                    )
    except Exception as exc:
        log.error("triage.auto_apply_outer_failed", run_id=run_id, error=str(exc))
        return

    log.info(
        "triage.auto_apply_complete",
        run_id=run_id,
        applied=applied,
        kept=kept,
        auto_kept_low_confidence=auto_kept_low_confidence,
        errors=len(errors),
    )

    try:
        from events import bus
        import time

        bus.emit(
            user_id,
            {
                "type": "auto_apply_complete",
                "ts": time.time(),
                "run_id": run_id,
                "applied": applied,
                "kept": kept,
                "auto_kept_low_confidence": auto_kept_low_confidence,
            },
        )
    except Exception:  # pragma: no cover - event bus must never fail
        pass


def finalize(state: TriageState) -> dict:
    counts = state.get("counts") or _counts(state.get("decisions") or [])
    cost = state.get("cost") or _cost(state)
    try:
        from db.session import create_db_session
        from graph.persistence import update_run

        with create_db_session() as session:
            update_run(
                session,
                run_id=state["run_id"],
                status="completed",
                items_decided=counts.get("total", 0),
                counts=counts,
                cost=cost,
            )
    except Exception as exc:  # pragma: no cover
        log.error("triage.finalize_persist_failed", error=str(exc))

    # A user cancel is terminal — update_run above refused to overwrite it, and
    # the returned state must agree with the run row.
    if _run_is_cancelled(state.get("run_id")):
        log.info("triage.finished_cancelled", run_id=state.get("run_id"))
        return {"status": "cancelled", "counts": counts, "cost": cost}

    # Auto-apply: apply all archive/digest decisions that are not needs_your_call.
    # Skip when dry_run is on; each apply is wrapped individually so one failure
    # never blocks the rest.
    if not state.get("dry_run", True):
        _auto_apply_decisions(state, counts)

    log.info(
        "triage.completed",
        run_id=state.get("run_id"),
        total=counts.get("total"),
        by_tier=counts.get("by_tier"),
        needs_your_call=counts.get("needs_your_call"),
        clusters=len(state.get("clusters") or []),
        llm_calls=cost.get("llm_calls"),
        tokens_in=cost.get("tokens_in"),
        tokens_out=cost.get("tokens_out"),
        usd=cost.get("usd"),
    )

    try:
        from events import bus

        bus.emit(
            state["user_id"],
            {
                "type": "run_completed",
                "ts": __import__("time").time(),
                "run_id": state["run_id"],
                "total_threads": counts.get("total", 0),
                "cost_usd": cost.get("usd", 0.0),
            },
        )
    except Exception:  # pragma: no cover
        pass

    return {"status": "completed", "counts": counts, "cost": cost}
