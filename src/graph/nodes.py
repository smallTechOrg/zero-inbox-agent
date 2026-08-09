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


def _run_async(coro):
    """Run an async LLM coroutine from a synchronous graph node."""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


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


def _call_llm(prompt: str, *, model: str | None = None) -> tuple[str, dict]:
    """Single completion through the shared client. Returns ``(text, usage_row)``."""
    from llm.client import get_llm_client

    result = _run_async(get_llm_client().call_model(prompt, model=model))
    return result.text, _usage_row(result, "deep_read", 1)


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
        return {"items": list(state["items"]), "error": None}
    try:
        from channels import get_adapter  # provided by the gmail-adapter slice

        adapter = get_adapter(
            user_id=state["user_id"], channel_account_id=state["channel_account_id"]
        )
        items = [
            i if isinstance(i, dict) else i.model_dump()
            for i in adapter.list_threads(limit=state.get("limit", 200))
        ]
        return {"items": items, "error": None}
    except Exception as exc:
        return {"error": f"fetch_items failed: {exc}"}


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

    categories = state.get("categories") or []
    category_keys = {c["key"] for c in categories}
    by_id = {i["id"]: i for i in batch}
    instructions = _prompt("classify.md").replace(
        "{categories}", _category_block(categories)
    )
    from llm.client import get_llm_client

    result = None
    last_error = "unknown error"
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
            "llm_calls": [],
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
    calls = [_usage_row(result.usage, "classify", len(batch))] if result.usage else []
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

    categories = state.get("categories") or []
    category_keys = {c["key"] for c in categories}
    template = _prompt("deep_read.md")
    candidates = _model_candidates(state)
    sender_stats = state.get("sender_stats") or {}

    decisions: list[dict] = []
    calls: list[dict] = []
    for item in queue:
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
                text, meta = _call_llm(prompt, model=model)
                raw = _extract_json(text, array=False)
                if not isinstance(raw, dict):
                    raise ValueError("expected a JSON object")
                verdict = _normalise_verdict(raw, item, category_keys)
                verdict["decided_by"] = "llm_deep"
                verdict["unsure"] = False
                decisions.append(verdict)
                calls.append(meta)
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


def _merge_decisions(state: TriageState) -> list[dict]:
    """One decision per item; the deepest tier wins. Undecided items degrade to keep."""
    best: dict[str, dict] = {}
    for decision in list(state.get("resolved") or []) + list(state.get("llm_decisions") or []):
        current = best.get(decision["item_id"])
        if current is None or _TIER_PRECEDENCE.get(
            decision["decided_by"], 0
        ) >= _TIER_PRECEDENCE.get(current["decided_by"], 0):
            best[decision["item_id"]] = decision

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
        decisions = apply_confidence_floor(_merge_decisions(state), floor)
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
    return {"status": "failed", "decisions": decisions, "counts": counts}


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
    return {"status": "completed", "counts": counts, "cost": cost}
