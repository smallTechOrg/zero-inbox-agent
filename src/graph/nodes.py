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

from graph import checkpoint
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
    import contextvars

    # Carry the CURRENT context into the pool thread. contextvars do NOT cross a
    # thread boundary on their own: without this, `llm.health.bind_run(...)` set
    # on the run's thread is invisible here, `active_run_id()` reads None, and
    # every provider_degraded / model_fallback event is emitted unattributed and
    # silently dropped instead of reaching the user's activity feed. Verified
    # empirically — plain `pool.submit(asyncio.run, ...)` sees None; via
    # `ctx.run` it sees the bound run.
    ctx = contextvars.copy_context()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(ctx.run, asyncio.run, wrapped).result(timeout=timeout + 5)


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


def _health():
    """``llm.health`` (slice 3) if it has landed, else ``None``.

    Imported defensively so this module keeps working — with the pre-Phase-6
    single-model behaviour — while the provider-resilience slice is still in flight.
    """
    try:
        from llm import health  # type: ignore[attr-defined]
    except ImportError:
        return None
    return health


def _model_candidates(state: TriageState) -> list[str]:
    """The chain of models this run may use NOW, current model first.

    ``llm.health.model_chain(preferred)`` is the full ordered in-family chain; it is
    sliced at the run's current chain position, so ``result[0]`` is always
    ``llm.health.current_model(run_id)`` — a model the run has already abandoned is
    never re-tried (spec/capabilities/durable-resumable-runs.md, Rule F).
    """
    preferred = (state.get("settings") or {}).get("llm_model")
    health = _health()
    chain_fn = getattr(health, "model_chain", None) if health else None
    if chain_fn is None:
        # Pre-Phase-6 behaviour: the user's model, then the provider default.
        return [preferred] if preferred else []
    chain = list(chain_fn(preferred) or [])
    run_id = state.get("run_id")
    snapshot_fn = getattr(health, "snapshot", None)
    if run_id and snapshot_fn is not None and chain:
        try:
            position = int((snapshot_fn(run_id) or {}).get("chain_position", 0) or 0)
        except Exception:  # pragma: no cover - health must never break the cascade
            position = 0
        # Position 0 = nothing abandoned yet, so the user's preferred model leads.
        # Every advance drops one more entry, permanently, for the rest of the run.
        if position > 0:
            chain = chain[position:] or chain[-1:]
    return chain


def _advance_model(state: TriageState, *, from_model: str | None, reason: str) -> str | None:
    """Rotate this run onto the next chain model. Returns it, or ``None`` if exhausted.

    Called once the current model's existing retry budget is exhausted, for **any**
    failure class including ``APIConnectionError``/timeouts: NVIDIA routes each model to
    its own backend pool, so a saturated pool for one model says nothing about the rest.
    The advance is per-run and sticks.
    """
    health = _health()
    advance = getattr(health, "advance_model", None) if health else None
    run_id = state.get("run_id")
    if advance is None or not run_id:
        return None
    try:
        to_model = advance(run_id, reason=reason)
    except Exception as exc:  # pragma: no cover - never break the cascade
        log.warning("triage.model_advance_failed", run_id=run_id, error=str(exc))
        return None
    if to_model is None:
        log.error("triage.model_chain_exhausted", run_id=run_id, reason=reason)
        return None
    log.warning(
        "llm.model_fallback", run_id=run_id, from_model=from_model, to_model=to_model,
        reason=reason,
    )
    try:
        from events.bus import emit_model_fallback

        emit_model_fallback(
            state["user_id"],
            run_id=run_id,
            from_model=str(from_model or ""),
            to_model=to_model,
            reason=reason,
        )
    except (ImportError, AttributeError):  # pragma: no cover - slice 2 not landed
        pass
    except Exception:  # pragma: no cover - the bus must never fail a run
        pass
    return to_model


def _circuit_open_error():
    """``llm.health.ProviderCircuitOpen`` if available, else a never-matching class."""
    health = _health()
    exc = getattr(health, "ProviderCircuitOpen", None) if health else None
    if isinstance(exc, type) and issubclass(exc, BaseException):
        return exc

    class _Never(Exception):
        pass

    return _Never


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
            if items_total is not None:
                # Never shrink the denominator — a resumed run only fetches what it
                # still has to decide.
                session.execute(
                    sa_update(run_cls)
                    .where(run_cls.id == run_id, run_cls.items_total < items_total)
                    .values(items_total=items_total)
                )
            if items_decided is not None:
                session.execute(
                    sa_update(run_cls)
                    .where(run_cls.id == run_id)
                    .values(items_decided=items_decided)
                )
            if decided_delta:
                session.execute(
                    sa_update(run_cls)
                    .where(run_cls.id == run_id)
                    .values(items_decided=run_cls.items_decided + decided_delta)
                )
    except Exception as exc:  # pragma: no cover - progress must never fail a run
        log.warning("triage.progress_write_failed", run_id=run_id, error=str(exc))


def _record_fetch_failures(run_id: str | None, *, fetched: int, failed: int) -> None:
    """Record an incomplete fetch on the run row so a partial run is never shown as whole.

    "Fetched 2,847 of 2,900 — 53 threads could not be read" is honest; silently
    triaging 2,847 and calling it the whole inbox is not.
    """
    if not run_id or failed <= 0:
        return
    try:
        from sqlalchemy import update as sa_update

        from db.session import create_db_session
        from graph.persistence import model_for

        run_cls = model_for("triage_runs")
        if run_cls is None:
            return
        message = (
            f"Fetched {fetched} of {fetched + failed} threads — {failed} could not be "
            f"read from Gmail and were not triaged. Re-run to pick them up."
        )
        with create_db_session() as session:
            session.execute(
                sa_update(run_cls).where(run_cls.id == run_id).values(error_message=message)
            )
    except Exception as exc:  # pragma: no cover - reporting must never fail a run
        log.warning("triage.fetch_failure_write_failed", run_id=run_id, error=str(exc))


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
    """Normalised items, headers + subject + <=200 char snippet only.

    On a resume, every thread this run has already decided is filtered out here — the
    single choke point every downstream tier queue is derived from — so an already
    decided thread is never re-sent to the LLM (Rule B).
    """
    if state.get("items"):
        items = _skip_already_decided(state, list(state["items"]))
        _record_progress(
            state.get("run_id"),
            items_total=len(items) + int(state.get("already_decided_count") or 0),
        )
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
            # Rule I2: a full-inbox fetch is minutes of otherwise silent work.
            log.info(
                "triage.page_fetched",
                run_id=run_id,
                page=page_num,
                fetched_so_far=fetched_so_far,
            )
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

        # Threads Gmail refused to hand over even after a retry on a fresh
        # connection. They are missing from `items`, so items_total would
        # silently under-count the mailbox — the user must be told instead.
        fetch_failed: list[str] = []

        def _on_fetch_failed(failed_ids: list[str]) -> None:
            fetch_failed.extend(failed_ids)
            try:
                from events import bus as _bus
                _bus.emit(user_id, {
                    "type": "fetch_failed",
                    "run_id": run_id,
                    "failed_in_page": len(failed_ids),
                    "failed_total": len(fetch_failed),
                })
            except Exception:  # pragma: no cover - bus must never fail a run
                pass

        list_kwargs = dict(
            limit=state.get("limit", 200),
            cancel_check=(lambda: _run_is_cancelled(run_id)) if run_id else None,
            after=after_dt,
            on_page=_on_fetch_page,
        )
        # Decide support by INSPECTING the signature, never by catching TypeError
        # around the call. A TypeError raised anywhere inside a 2,900-thread
        # fetch (a bad cancel_check, a normalize bug) would otherwise be
        # swallowed and silently trigger a COMPLETE second full-inbox re-fetch —
        # double the Gmail quota and runtime, with the failure callback lost.
        import inspect

        if "on_fetch_failed" in inspect.signature(adapter.list_threads).parameters:
            list_kwargs["on_fetch_failed"] = _on_fetch_failed
        fetched = adapter.list_threads(**list_kwargs)
        items = [i if isinstance(i, dict) else i.model_dump() for i in fetched]
        mailbox_total = len(items)
        items = _skip_already_decided(state, items)
        # The progress bar's denominator, written the moment the total is known —
        # not at the end of the run (spec/api.md: GET /api/runs is polled at 1s).
        _record_progress(state.get("run_id"), items_total=mailbox_total)
        if fetch_failed:
            log.warning(
                "gmail.fetch_incomplete",
                run_id=run_id,
                fetched=len(items),
                failed=len(fetch_failed),
            )
            _record_fetch_failures(run_id, fetched=len(items), failed=len(fetch_failed))
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


def _skip_already_decided(state: TriageState, items: list[dict]) -> list[dict]:
    """Drop threads this run has already decided (resume). Never re-classified."""
    decided = set(state.get("already_decided_item_ids") or [])
    if not decided:
        return items
    remaining = [
        item
        for item in items
        if item.get("id") not in decided
        and (item.get("external_thread_id") or item.get("id")) not in decided
    ]
    log.info(
        "triage.resume_skipped",
        run_id=state.get("run_id"),
        skipped=len(items) - len(remaining),
        remaining=len(remaining),
    )
    return remaining


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


# ------------------------------------------- Rule I2: not one beat without a log
#
# These lines exist so the live feed never goes quiet. They are LOGGED, never
# hand-emitted: `observability.logging.activity_bus_processor` bridges every
# structlog line carrying a bound `user_id` onto the user's SSE stream, so a new
# log line can never drift out of feed coverage the way a hand-placed
# `bus.emit(...)` call site did (Gmail 429s and LLM retries were invisible for two
# phases). `run_id`/`user_id` are bound once per run in `graph.runner`.


def _tier_started(state: TriageState, *, tier: int, name: str, **fields) -> None:
    log.info("triage.tier_started", run_id=state.get("run_id"), tier=tier, tier_name=name, **fields)


def _tier_finished(state: TriageState, *, tier: int, name: str, **fields) -> None:
    log.info("triage.tier_finished", run_id=state.get("run_id"), tier=tier, tier_name=name, **fields)


def _batch_position(state: TriageState, batch: list[dict]) -> tuple[int, int]:
    """``(batch_n, batch_total)`` for one tier-3 batch — 1-based, for the feed header.

    Tier 3 fans out, so a batch has no intrinsic index; it is located by its first
    thread inside ``state["batches"]``. Unknown position degrades to ``(0, total)``
    rather than raising: a log line must never be able to fail a run.
    """
    batches = state.get("batches") or []
    total = len(batches)
    if not batch or not batches:
        return 0, total
    head = batch[0].get("id")
    for index, candidate in enumerate(batches, start=1):
        if candidate and candidate[0].get("id") == head:
            return index, total
    return 0, total


def redact_items(state: TriageState) -> dict:
    """The single egress chokepoint — nothing reaches an LLM unredacted."""
    items = _redact_all(list(state.get("items") or []))
    return {"items": items}


def apply_deterministic_rules(state: TriageState) -> dict:
    _tier_started(state, tier=1, name="deterministic_rules", queued=len(state.get("items") or []))
    decisions, unresolved = rules_tool.apply_rules(
        state.get("items") or [], state.get("rules") or []
    )
    log.info(
        "triage.tier1", run_id=state.get("run_id"), resolved=len(decisions),
        remaining=len(unresolved),
    )
    _tier_finished(state, tier=1, name="deterministic_rules", resolved=len(decisions),
                   remaining=len(unresolved))
    # Durable the instant the tier decides: the rows land provisional and the
    # per-thread event is emitted from the same checkpoint.
    checkpoint.record_batch(state, decisions, tier="rule")
    return {"resolved": decisions, "llm_queue": unresolved}


def apply_sender_history(state: TriageState) -> dict:
    _tier_started(state, tier=2, name="sender_history", queued=len(state.get("llm_queue") or []))
    decisions, unresolved = rules_tool.apply_sender_history(
        state.get("llm_queue") or [], state.get("sender_stats") or {}
    )
    log.info(
        "triage.tier2", run_id=state.get("run_id"), resolved=len(decisions),
        remaining=len(unresolved),
    )
    _tier_finished(state, tier=2, name="sender_history", resolved=len(decisions),
                   remaining=len(unresolved))
    checkpoint.record_batch(state, decisions, tier="sender_history")
    return {"resolved": decisions, "llm_queue": unresolved}


def prepare_llm_batches(state: TriageState) -> dict:
    batches = _chunk(state.get("llm_queue") or [])
    # Tiers 1-2 have already checkpointed their own decisions and bumped
    # items_decided atomically — writing an absolute count here would clobber a
    # resumed run's carried-over progress.
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
    circuit_open = _circuit_open_error()
    batch_n, batch_total = _batch_position(state, batch)
    _tier_started(
        state, tier=3, name="llm_classify", batch_n=batch_n, batch_total=batch_total,
        batch_size=len(batch),
    )
    for model in _model_candidates(state) or [None]:
        # Logged BEFORE the call, not after: a 60s tier-3 batch is exactly the dead-air
        # window that made a working run look hung.
        log.info(
            "triage.batch_dispatched",
            run_id=run_id,
            batch_n=batch_n,
            batch_total=batch_total,
            batch_size=len(batch),
            model=model,
        )
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
            log.info(
                "triage.batch_returned",
                run_id=run_id,
                batch_n=batch_n,
                batch_total=batch_total,
                batch_size=len(batch),
                model=model,
                verdicts=len(result.results),
            )
            break
        except circuit_open as exc:
            # The provider is circuit-broken for every model this run can still
            # reach. Do NOT degrade the batch into keep-everything rows — route to
            # the error handler so the run closes as `resumable` with every already
            # decided thread durable (Rule E).
            log.error("triage.tier3_circuit_open", run_id=run_id, model=model, error=str(exc))
            return {"error": f"provider circuit open: {exc}", "llm_calls": failed_calls}
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
            # This model's retry budget is spent. Rotate the whole run onto the next
            # chain entry — for every failure class, connection errors included — and
            # carry on rather than failing the batch.
            _advance_model(
                state,
                from_model=model,
                reason=f"{type(exc).__name__}: {last_error}"[:200],
            )

    if result is None:
        log.error("triage.tier3_failed", run_id=state.get("run_id"), error=last_error)
        degraded = _degraded(batch, last_error)
        checkpoint.record_batch(state, degraded, tier="error", llm_calls=failed_calls, items=batch)
        _tier_finished(
            state, tier=3, name="llm_classify", batch_n=batch_n,
            batch_total=batch_total, decided=0, failed=len(batch),
        )
        return {
            "llm_decisions": degraded,
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
    # Durable + visible in one step: the batch's rows land provisional and each
    # thread emits its own classification event.
    checkpoint.record_batch(state, decisions, tier="llm", llm_calls=calls, items=batch)
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
    _tier_finished(
        state, tier=3, name="llm_classify", batch_n=batch_n, batch_total=batch_total,
        decided=len(decisions), unsure=len(deep),
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
    candidates = _model_candidates(state) or [None]
    circuit_open = _circuit_open_error()
    sender_stats = state.get("sender_stats") or {}

    _tier_started(state, tier=4, name="deep_read", queued=len(queue))
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
                # all at once) — each is checkpointed and emitted on its own, so the
                # numerator advances thread by thread.
                checkpoint.record_batch(state, [verdict], tier="llm_deep", llm_calls=metas, items=[item])
                break
            except circuit_open as exc:
                # Chain exhausted and circuit-broken: stop spending, keep the batch
                # verdict (low confidence -> needs_your_call), never archive.
                log.error(
                    "triage.tier4_circuit_open", run_id=run_id, item_id=item["id"],
                    error=str(exc),
                )
                return {"llm_decisions": decisions, "llm_calls": calls}
            except Exception as exc:
                # The batch verdict stands: low confidence -> needs_your_call, never archive.
                log.warning(
                    "triage.tier4_failed", run_id=state.get("run_id"), item_id=item["id"],
                    model=model, error=str(exc),
                )
                _advance_model(
                    state, from_model=model, reason=f"{type(exc).__name__}: {exc}"[:200]
                )

    log.info("triage.tier4", run_id=state.get("run_id"), deep_reads=len(decisions))
    _tier_finished(state, tier=4, name="deep_read", deep_reads=len(decisions))
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


def _carry_forward_provisional(state: TriageState) -> tuple[list[dict], list[dict]]:
    """A resumed run's already-persisted provisional rows, ready for the reviewer.

    Rule B: the interrupted leg's threads must reach the second-pass reviewer exactly
    once, together with the ones this leg decided — otherwise they would either stay
    provisional forever (never appliable) or be upgraded without ever being reviewed.
    """
    run_id = state.get("run_id")
    if not run_id or not state.get("already_decided_item_ids"):
        return [], []
    try:
        from db.session import create_db_session
        from graph.persistence import load_provisional_for_review

        with create_db_session() as session:
            return load_provisional_for_review(
                session,
                run_id=run_id,
                user_id=state["user_id"],
                only_item_ids=set(state.get("already_decided_item_ids") or []),
            )
    except Exception as exc:  # pragma: no cover - never fail the run over a carry-over
        log.warning("triage.carry_forward_failed", run_id=run_id, error=str(exc))
        return [], []


def cluster_decisions(state: TriageState) -> dict:
    try:
        floor = float(
            (state.get("settings") or {}).get("confidence_floor", DEFAULT_CONFIDENCE_FLOOR)
        )
        cancelled = _run_is_cancelled(state.get("run_id"))
        carried_items, carried_decisions = _carry_forward_provisional(state)
        items = list(state.get("items") or []) + carried_items
        decisions = apply_confidence_floor(
            _merge_decisions(state, cancelled=cancelled) + carried_decisions, floor
        )
        clusters = clustering.cluster(
            decisions, items, categories=state.get("categories") or []
        )
        log.info(
            "triage.clustered", run_id=state.get("run_id"), decisions=len(decisions),
            clusters=len(clusters),
        )
        # Rule I2. `cluster_decisions` is the node immediately before the never-miss
        # chain (align_to_category_default -> second_pass_reviewer), which lives in
        # `graph/nodes_review.py` — a file this slice does not own. The matching
        # `triage.reviewer_finished` is logged by `graph.persistence.finalise_review`.
        log.info(
            "triage.reviewer_started",
            run_id=state.get("run_id"),
            to_review=sum(
                1
                for d in decisions
                if d.get("proposed_action") == "archive"
                and d.get("status") != "needs_your_call"
            ),
            decisions=len(decisions),
        )
        return {
            "decisions": decisions,
            "clusters": clusters,
            "items": items,
            "error": None,
        }
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
    """Idempotent finalisation — no longer the first write.

    Every tier checkpointed its own rows as ``provisional`` while the run was in
    flight (``graph.checkpoint``). This node writes the clusters, back-fills cluster
    membership and the reviewer's final verdict onto the existing rows, inserts
    anything that never made it through a checkpoint, and upgrades the whole run's
    ``review_state`` — the point at which the decisions become final.
    """
    try:
        from db.session import create_db_session
        from graph.persistence import (
            persist_run_results,
            run_cost_totals,
            upgrade_review_state,
        )

        floor = float(
            (state.get("settings") or {}).get("confidence_floor", DEFAULT_CONFIDENCE_FLOOR)
        )
        decisions = apply_confidence_floor(state.get("decisions") or [], floor)
        review_failed = set(state.get("review_failed_item_ids") or [])
        for decision in decisions:
            decision["review_state"] = (
                "review_failed" if decision["item_id"] in review_failed else "reviewed"
            )
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
            # Safety sweep: nothing may finish the graph still provisional. Any row
            # the reviewer never saw (e.g. checkpointed by a tier after the reviewer
            # ran) is upgraded here, so no decision is left permanently un-appliable.
            upgrade_review_state(session, run_id=state["run_id"], state="reviewed")
            cost = run_cost_totals(session, state["run_id"])
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
    """Degradation always keeps mail visible — and never destroys resumable work.

    A run that already decided something ends ``resumable``, not ``failed``: every
    decided thread is durable (``graph.checkpoint``) and the undecided ones are left
    undecided so a resume picks them up. Only a run with nothing persisted is
    ``failed`` — there is nothing to resume.
    """
    error = state.get("error") or "unknown error"
    decided_so_far = _persisted_decision_count(state.get("run_id"))
    resumable = decided_so_far > 0
    # A resumable run must NOT synthesize "no verdict" rows for the threads it never
    # reached — that would decide them as errors and there would be nothing to resume.
    decisions = apply_confidence_floor(
        _merge_decisions(state, cancelled=resumable), 1.1  # everything to the user
    )
    for decision in decisions:
        decision.setdefault("review_state", "reviewed")
    counts = _counts(decisions)
    status = "resumable" if resumable else "failed"
    mailbox_total = max(
        len(state.get("items") or []) + int(state.get("already_decided_count") or 0),
        decided_so_far,
    )
    message = (
        f"Interrupted at {decided_so_far} of {mailbox_total} threads — nothing was "
        f"left half-applied. Resume to continue. ({error})"
        if resumable
        else error
    )
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
                status=status,
                error=message,
            )
    except Exception as exc:  # pragma: no cover - the run is already failing
        log.error("triage.persist_on_error_failed", error=str(exc))
    log.error("triage.failed", run_id=state.get("run_id"), error=error, status=status)

    try:
        from events import bus

        bus.emit(
            state["user_id"],
            {
                "type": "error",
                "ts": __import__("time").time(),
                "run_id": state.get("run_id"),
                "message": message,
            },
        )
    except Exception:  # pragma: no cover
        pass

    if resumable:
        try:
            from events.bus import emit_run_resumable

            emit_run_resumable(
                state["user_id"],
                run_id=state.get("run_id") or "",
                items_total=len(state.get("items") or []) + decided_so_far,
                items_decided=decided_so_far,
                reason=error,
            )
        except (ImportError, AttributeError):  # pragma: no cover - slice 2 not landed
            pass
        except Exception:  # pragma: no cover
            pass

    return {"status": status, "decisions": decisions, "counts": counts}


def _persisted_decision_count(run_id: str | None) -> int:
    """How many decisions this run has already made durable."""
    if not run_id:
        return 0
    try:
        from sqlalchemy import func, select as sa_select

        from db.session import create_db_session
        from graph.persistence import model_for

        decision_cls = model_for("decisions")
        if decision_cls is None:
            return 0
        with create_db_session() as session:
            return int(
                session.execute(
                    sa_select(func.count(decision_cls.id)).where(
                        decision_cls.run_id == run_id
                    )
                ).scalar_one()
                or 0
            )
    except Exception as exc:  # pragma: no cover - never fail the error handler
        log.warning("triage.decision_count_failed", run_id=run_id, error=str(exc))
        return 0


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


#: An ``apply_progress`` event every N applied decisions (plus one at the end), so a
#: long apply pass is visibly moving rather than a frozen bar (Rule D8).
APPLY_PROGRESS_EVERY = 25

#: A decision in one of these states has already left the appliable set for good.
_TERMINAL_STATUSES = ("applied", "undone", "rejected")


def _empty_apply_ledger(dry_run: bool) -> dict:
    """The ledger shape pinned in spec/roadmap.md. Always returned, never omitted."""
    return {
        "applied": 0,
        "already_applied": 0,
        "not_reviewed": 0,
        "kept": 0,
        "needs_your_call": 0,
        "below_threshold": 0,
        "failed": 0,
        "failures": [],
        "distance_to_zero": 0,
        "apply_failed_reason": None,
        "dry_run": bool(dry_run),
    }


def _apply_session():
    """The session used by the apply pass — a seam so the outer-failure path is testable."""
    from db.session import create_db_session

    return create_db_session()


def distance_to_zero(run_id: str, user_id: str) -> int:
    """Decisions the agent itself decided should leave the inbox and that are still in it.

    ``count(decisions WHERE run_id = :id AND autonomy_state = 'auto_act'
    AND status != 'applied')``. On a healthy completed run this is 0. Best-effort:
    it is read in failure paths, so it never raises.
    """
    try:
        from sqlalchemy import func, select as sa_select

        from db.session import create_db_session
        from graph.persistence import model_for

        decision_cls = model_for("decisions")
        if decision_cls is None or not hasattr(decision_cls, "autonomy_state"):
            return 0
        with create_db_session() as session:
            return int(
                session.execute(
                    sa_select(func.count(decision_cls.id)).where(
                        decision_cls.run_id == run_id,
                        decision_cls.user_id == user_id,
                        decision_cls.autonomy_state == "auto_act",
                        decision_cls.status != "applied",
                    )
                ).scalar_one()
                or 0
            )
    except Exception as exc:  # pragma: no cover - never fail a run over a count
        log.warning("triage.distance_to_zero_failed", run_id=run_id, error=str(exc))
        return 0


def _emit_apply_progress(user_id: str, **payload) -> None:
    """Slice 3 owns the typed helper; import defensively and never raise."""
    try:
        from events.bus import emit_apply_progress
    except (ImportError, AttributeError):  # pragma: no cover - slice 3 not landed
        return
    try:
        emit_apply_progress(user_id, **payload)
    except Exception:  # pragma: no cover - the bus must never fail an apply
        pass


def apply_run_decisions(
    *, run_id: str, user_id: str, channel_account_id: str, dry_run: bool
) -> dict:
    """Apply every decision this run is allowed to act on, and always report a ledger.

    Rule D of spec/capabilities/drive-to-inbox-zero.md. A decision is applied only when
    ALL of these hold:

    * ``review_state == "reviewed"`` — the Phase 6 never-miss gate, which
      ``tools.actions.apply_decision`` re-checks first and which is **never** bypassed;
    * ``autonomy_state == "auto_act"`` — the per-category autonomy policy said so;
    * ``proposed_action in ("archive", "digest")``;
    * ``status not in ("needs_your_call", "applied", "undone", "rejected")``.

    ``force=True`` is NEVER passed: a ``keep`` is resolved at decision time by
    ``align_to_category_default``, never force-archived here (Rule D2). ``review_state``
    is NEVER written by this function — only the never-miss chain may set it (Rule D3).

    **It never returns early without a ledger and never raises** (Rule D6). The two
    paths that used to `return` silently — a failure building the Gmail mutator and the
    outer ``except`` — now record ``apply_failed_reason``, which ``finalize`` turns into
    ``triage_runs.error_message``, a ``run_apply_failed`` event and ``apply_ok=false``.
    """
    ledger = _empty_apply_ledger(dry_run)
    try:
        _apply_pass(
            ledger,
            run_id=run_id,
            user_id=user_id,
            channel_account_id=channel_account_id,
            dry_run=dry_run,
        )
    except Exception as exc:
        # The outer path. This used to `return` silently too.
        ledger["apply_failed_reason"] = f"{type(exc).__name__}: {exc}"
        log.error("triage.auto_apply_outer_failed", run_id=run_id, error=str(exc))

    # Re-read from the database rather than trusting the loop's arithmetic: this is
    # the number the gate asserts and the UI renders, and on every path — including
    # every failure path — it must be the true remaining count. The tail after an
    # early `return` inside the pass is exactly what used to be skipped.
    measured = distance_to_zero(run_id, user_id)
    if measured or not ledger["apply_failed_reason"]:
        ledger["distance_to_zero"] = measured
    return ledger


def _apply_pass(
    ledger: dict, *, run_id: str, user_id: str, channel_account_id: str, dry_run: bool
) -> None:
    """The apply pass itself. Fills ``ledger`` in place; may raise (the caller records)."""
    from channels.base import ChannelError, DryRunViolation
    from sqlalchemy import select as sa_select

    from graph.persistence import model_for
    # Imported as a module, not as a name, so the call site is a single
    # observable seam (`tools.actions.apply_decision`) that the gate spies on to
    # assert `force=False` on every call.
    from tools import actions as actions_module
    from tools.actions import ActionsError

    decision_cls = model_for("decisions")
    if decision_cls is None:  # pragma: no cover - schema always present
        ledger["apply_failed_reason"] = "SchemaMissing: no `decisions` table"
        return

    with _apply_session() as session:
        rows = list(
            session.execute(
                sa_select(decision_cls).where(
                    decision_cls.run_id == run_id,
                    decision_cls.user_id == user_id,
                )
            ).scalars()
        )

        eligible = []
        for row in rows:
            status = getattr(row, "status", "proposed")
            if status == "applied":
                ledger["already_applied"] += 1
                continue
            if status == "needs_your_call":
                ledger["needs_your_call"] += 1
                continue
            if status in _TERMINAL_STATUSES:
                ledger["kept"] += 1
                continue
            if getattr(row, "proposed_action", "keep") not in ("archive", "digest"):
                ledger["kept"] += 1
                continue
            if getattr(row, "review_state", "reviewed") != "reviewed":
                # The Phase 6 gate. Counted, left completely alone — this function
                # never upgrades review_state to get past it.
                ledger["not_reviewed"] += 1
                continue
            autonomy_state = getattr(row, "autonomy_state", None)
            if autonomy_state != "auto_act":
                # `below_threshold` is its own bucket; everything else (including a
                # NULL/unclassified row) stays in the inbox and is counted as kept.
                if autonomy_state == "below_threshold":
                    ledger["below_threshold"] += 1
                else:
                    ledger["kept"] += 1
                continue
            eligible.append(row)

        ledger["distance_to_zero"] = len(eligible)

        if dry_run:
            # Rule D4 — absolute. Zero mutations, zero Gmail credentials built.
            log.info(
                "triage.apply_skipped_dry_run",
                run_id=run_id,
                would_apply=len(eligible),
            )
            return

        if not eligible:
            return

        try:
            mutator, label_lookup = _build_mutator_for_user(
                user_id, channel_account_id, session
            )
        except Exception as exc:
            # Silent-abort path 1 of 2 on run `fbeed060`: this used to log and
            # `return` with no ledger, so a run that applied nothing reported
            # itself `completed`.
            #
            # MEASURED (tests/integration/test_apply_diagnosis.py, run read-only
            # against the very account of `fbeed060` — connection
            # 3ef143f1 / user 6b4ab0f4): the stored refresh token decrypts and
            # `_build_mutator_for_user` + `labels().list()` SUCCEED today. So this
            # path is EXCLUDED as the cause of that run. The remaining candidates
            # (the outer `except`, or a Gmail failure on every one of the 615
            # archives) are indistinguishable from the persisted state — because
            # the old code recorded nothing about either. That un-diagnosability
            # IS the defect: from here on, every path names itself in
            # `apply_failed_reason` / `failures[]` / `triage_runs.error_message`.
            ledger["apply_failed_reason"] = f"{type(exc).__name__}: {exc}"
            log.error(
                "triage.auto_apply_build_mutator_failed",
                run_id=run_id,
                error=str(exc),
            )
            return

        total = len(eligible)
        for index, row in enumerate(eligible, start=1):
            decision_id = row.id
            try:
                # `approved` is the apply-eligibility status; review_state is
                # untouched, so the never-miss gate still binds inside
                # apply_decision and an un-reviewed row can never slip through.
                row.status = "approved"
                session.flush()
                actions_module.apply_decision(
                    session,
                    user_id,
                    decision_id,
                    mutator=mutator,
                    label_lookup=label_lookup,
                    dry_run=False,
                    force=False,  # Rule D2 — never a side door around a `keep`.
                )
                session.commit()
                ledger["applied"] += 1
            except (ActionsError, ChannelError, DryRunViolation) as exc:
                # ActionsError covers NeedsYourCall / NotApproved / NotArchivable /
                # NotReviewed — every business-rule refusal, none of them bypassed.
                session.rollback()
                ledger["failed"] += 1
                ledger["failures"].append(
                    {"decision_id": str(decision_id), "error": f"{type(exc).__name__}: {exc}"}
                )
                log.warning(
                    "triage.auto_apply_decision_failed",
                    run_id=run_id,
                    decision_id=str(decision_id),
                    error=str(exc),
                )
            except Exception as exc:
                # Rule D7 — one thread's failure never blocks the rest.
                session.rollback()
                ledger["failed"] += 1
                ledger["failures"].append(
                    {"decision_id": str(decision_id), "error": f"{type(exc).__name__}: {exc}"}
                )
                log.warning(
                    "triage.auto_apply_unexpected_error",
                    run_id=run_id,
                    decision_id=str(decision_id),
                    error=str(exc),
                )
            if index % APPLY_PROGRESS_EVERY == 0 or index == total:
                log.info(
                    "triage.apply_progress",
                    run_id=run_id,
                    applied=ledger["applied"],
                    total_to_apply=total,
                    failed=ledger["failed"],
                )
                _emit_apply_progress(
                    user_id,
                    run_id=run_id,
                    applied=ledger["applied"],
                    total_to_apply=total,
                    failed=ledger["failed"],
                )



def _auto_apply_decisions(state: TriageState, counts: dict) -> dict:
    """Thin wrapper over :func:`apply_run_decisions` for the graph's ``finalize``.

    Keeps the ``triage.auto_apply_complete`` log line and the ``auto_apply_complete``
    SSE event (extended with the full ledger) so no existing subscriber breaks.
    """
    run_id = state.get("run_id") or ""
    user_id = state["user_id"]
    ledger = apply_run_decisions(
        run_id=run_id,
        user_id=user_id,
        channel_account_id=state.get("channel_account_id") or "",
        dry_run=bool(state.get("dry_run", True)),
    )
    counts["apply"] = ledger

    log.info(
        "triage.auto_apply_complete",
        run_id=run_id,
        applied=ledger["applied"],
        kept=ledger["kept"],
        # Retained key name: existing subscribers read it.
        auto_kept_low_confidence=ledger["needs_your_call"],
        not_reviewed=ledger["not_reviewed"],
        below_threshold=ledger["below_threshold"],
        already_applied=ledger["already_applied"],
        errors=ledger["failed"],
        distance_to_zero=ledger["distance_to_zero"],
        apply_failed_reason=ledger["apply_failed_reason"],
        dry_run=ledger["dry_run"],
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
                "applied": ledger["applied"],
                "kept": ledger["kept"],
                "auto_kept_low_confidence": ledger["needs_your_call"],
                "ledger": {k: v for k, v in ledger.items() if k != "failures"},
            },
        )
    except Exception:  # pragma: no cover - event bus must never fail
        pass

    return ledger


def _remainder_ledger(run_id: str, user_id: str) -> dict | None:
    """Slice 3 owns ``graph.remainder``; import defensively and never raise."""
    try:
        from graph.remainder import remainder_ledger
    except (ImportError, AttributeError):  # pragma: no cover - slice 3 not landed
        return None
    try:
        from db.session import create_db_session

        with create_db_session() as session:
            return remainder_ledger(session, run_id=run_id, user_id=user_id)
    except Exception as exc:  # pragma: no cover - a report must never fail a run
        log.warning("triage.remainder_ledger_failed", run_id=run_id, error=str(exc))
        return None


def _persist_counts(run_id: str, counts: dict) -> None:
    """Write ``counts`` onto the run row. Never raises — a report never fails a run."""
    try:
        from db.session import create_db_session
        from graph.persistence import update_run

        with create_db_session() as session:
            update_run(session, run_id=run_id, counts=counts)
    except Exception as exc:  # pragma: no cover
        log.warning("triage.apply_counts_persist_failed", run_id=run_id, error=str(exc))


def _apply_failure_sentence(ledger: dict) -> str:
    """The human-readable line written to ``triage_runs.error_message`` (Rule D6)."""
    remaining = ledger["distance_to_zero"]
    if ledger["apply_failed_reason"]:
        return (
            f"Archiving failed and {remaining} thread(s) the agent had decided to "
            f"archive are still in your inbox: {ledger['apply_failed_reason']}. "
            "Nothing was left half-applied — use Retry archiving to try again."
        )
    return (
        f"{remaining} thread(s) the agent had decided to archive are still in your "
        f"inbox ({ledger['failed']} failed on Gmail). Use Retry archiving to try again."
    )


def finalize(state: TriageState) -> dict:
    counts = state.get("counts") or _counts(state.get("decisions") or [])
    cost = state.get("cost") or _cost(state)
    run_id = state.get("run_id") or ""
    user_id = state["user_id"]
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

    # Apply. Always called — `dry_run` is enforced INSIDE apply_run_decisions so the
    # ledger exists either way, and a run that applied nothing can never again be
    # rendered as a clean success (Rule D6).
    ledger = _auto_apply_decisions(state, counts)
    # The apply ledger MUST be on the run row before the remainder is computed:
    # `remainder_ledger` re-reads `run.counts["apply"]` from the DB to source
    # `apply_failed_reason`/`apply_ok`. Persisting it afterwards produced a snapshot
    # that always read `apply_ok: true` even when the apply pass genuinely failed —
    # exactly the false-clean this phase exists to eliminate.
    _persist_counts(run_id, counts)
    remainder = _remainder_ledger(run_id, user_id)
    if remainder is not None:
        counts["remainder"] = remainder

    apply_failed = bool(ledger["apply_failed_reason"]) or ledger["distance_to_zero"] > 0
    # Rule D4: a dry run archives nothing by design — that is not a failure.
    if apply_failed and not ledger["dry_run"]:
        message = _apply_failure_sentence(ledger)
        log.error(
            "triage.run_apply_failed",
            run_id=run_id,
            reason=ledger["apply_failed_reason"] or "not_all_applied",
            distance_to_zero=ledger["distance_to_zero"],
            failed=ledger["failed"],
        )
        try:
            from db.session import create_db_session
            from graph.persistence import update_run

            with create_db_session() as session:
                update_run(session, run_id=run_id, error=message)
        except Exception as exc:  # pragma: no cover
            log.warning("triage.apply_error_message_failed", run_id=run_id, error=str(exc))
        try:
            from events.bus import emit_run_apply_failed

            emit_run_apply_failed(
                user_id,
                run_id=run_id,
                reason=ledger["apply_failed_reason"] or "not_all_applied",
                distance_to_zero=ledger["distance_to_zero"],
            )
        except (ImportError, AttributeError):  # pragma: no cover - slice 3 not landed
            pass
        except Exception:  # pragma: no cover - the bus never fails a run
            pass

    # Rule E4 — the report is always produced, healthy or not.
    log.info(
        "triage.inbox_zero_report",
        run_id=run_id,
        applied=ledger["applied"],
        distance_to_zero=ledger["distance_to_zero"],
        remainder=(remainder or {}).get("remainder"),
        apply_ok=not apply_failed,
    )
    try:
        from events.bus import emit_inbox_zero_report

        emit_inbox_zero_report(
            user_id,
            run_id=run_id,
            applied=ledger["applied"],
            distance_to_zero=ledger["distance_to_zero"],
            remainder=(remainder or {}).get("remainder") or {},
        )
    except (ImportError, AttributeError):  # pragma: no cover - slice 3 not landed
        pass
    except Exception:  # pragma: no cover
        pass

    # Persist the remainder ledger onto the run row (apply was persisted above).
    _persist_counts(run_id, counts)

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
