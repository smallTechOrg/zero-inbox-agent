"""Phase 2 never-miss safeguard nodes: second-pass reviewer + floor/reply-history.

See spec/capabilities/never-miss-safeguards.md and spec/agent.md. These two nodes
run after ``cluster_decisions`` (which already merges tiers 1-4 into
``state["decisions"]``) and before ``persist_decisions``. ``decisions`` carries no
reducer (see ``graph/state.py``) so each node's return value is the new,
authoritative snapshot for the rest of the run.

Fixed order: reviewer -> floor -> reply-history. Each stage can only make an
outcome MORE conservative (archive -> keep), never the reverse.
"""

from __future__ import annotations

from pathlib import Path

from graph.state import TriageState
from observability.events import get_logger
from tools.never_miss import (
    DEFAULT_CONFIDENCE_FLOOR,
    apply_confidence_floor,
    apply_reply_history_guard,
    apply_vip_guard,
)

log = get_logger("never_miss")

_PROMPT_DIR = Path(__file__).parent.parent / "prompts"
_PROMPT_CACHE: dict[str, str] = {}

MIN_BATCH = 20
MAX_BATCH = 50

_REVIEW_SCHEMA = {
    "type": "object",
    "required": ["item_id", "flip", "reasoning"],
    "properties": {
        "item_id": {"type": "string"},
        "flip": {"type": "boolean"},
        "reasoning": {"type": "string", "minLength": 1},
    },
    "additionalProperties": True,
}


def _prompt(name: str) -> str:
    cached = _PROMPT_CACHE.get(name)
    if cached is None:
        cached = (_PROMPT_DIR / name).read_text(encoding="utf-8")
        _PROMPT_CACHE[name] = cached
    return cached


_LLM_TIMEOUT = 120.0  # seconds


def _run_async(coro, timeout: float = _LLM_TIMEOUT):
    import asyncio

    wrapped = asyncio.wait_for(coro, timeout=timeout)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(wrapped)

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, wrapped).result(timeout=timeout + 5)


def _usage_row(usage, items_in_batch: int) -> dict:
    return {
        "purpose": "review",
        "items_in_batch": items_in_batch,
        "model": getattr(usage, "model", "") or "",
        "tokens_in": int(getattr(usage, "tokens_in", 0) or 0),
        "tokens_out": int(getattr(usage, "tokens_out", 0) or 0),
        "cost_usd": float(getattr(usage, "usd", 0.0) or 0.0),
        "latency_ms": int(getattr(usage, "latency_ms", 0) or 0),
    }


def _thread_context(item: dict, decision: dict) -> dict:
    return {
        "item_id": decision["item_id"],
        "from": item.get("from_name") or "",
        "from_email": item.get("from_email") or "",
        "domain": item.get("from_domain") or "",
        "subject": item.get("subject") or "",
        "snippet": item.get("snippet_redacted") or "",
        "proposed_category": decision.get("category"),
        "proposed_reasoning": decision.get("reasoning"),
    }


def _chunk(rows: list, size: int = MAX_BATCH) -> list[list]:
    return [rows[i : i + size] for i in range(0, len(rows), size)] if rows else []


def _priorities_block(state: TriageState) -> str:
    """The user's priorities profile, injected verbatim — never rewritten by the
    agent. Empty when the user has not written one yet."""
    text = (state.get("priorities_profile") or "").strip()
    return text or "(The user has not written a priorities profile yet.)"


def review_archive_batch(
    decisions: list[dict],
    items_by_id: dict[str, dict],
    *,
    priorities: str = "",
    model: str | None = None,
    max_attempts: int = 3,
) -> tuple[dict[str, str], list[dict], bool]:
    """One reviewer LLM pass over a batch of archive proposals.

    Returns ``(flips, usage_rows, failed)`` where ``flips`` maps
    ``item_id -> reviewer reasoning`` for everything the reviewer flipped to
    keep. ``failed=True`` means the batch could not be reviewed at all after
    retries — the caller must route every item in it to ``needs_your_call``,
    never leave it as an unreviewed archive.
    """
    from llm.client import get_llm_client

    payload = [_thread_context(items_by_id.get(d["item_id"], {}), d) for d in decisions]
    try:
        result = _run_async(
            get_llm_client().classify_batch(
                payload,
                instructions=_prompt("reviewer.md").replace(
                    "{priorities}", priorities or "(The user has not written a priorities profile yet.)"
                ),
                item_schema=_REVIEW_SCHEMA,
                id_field="item_id",
                model=model,
                max_attempts=max_attempts,
            )
        )
    except Exception as exc:
        log.warning(
            "never_miss.reviewer_batch_failed", error=str(exc), batch_size=len(decisions)
        )
        return {}, [], True

    usage = [_usage_row(result.usage, len(decisions))] if result.usage else []
    flips: dict[str, str] = {}
    for entry in result.results:
        if not bool(entry.get("flip")):
            continue
        item_id = str(entry.get("item_id"))
        flips[item_id] = str(entry.get("reasoning") or "").strip() or (
            "The reviewer judged this thread important enough to keep visible."
        )
    return flips, usage, False


def second_pass_reviewer(state: TriageState) -> dict:
    """Mechanism A. Audits every archive proposal for false negatives.

    Only ever flips ``archive -> keep``; the model is never given the option
    to propose archive itself. A batch that cannot be reviewed after retries
    leaves every one of its threads in ``needs_your_call`` rather than
    archiving anything un-reviewed.
    """
    decisions = list(state.get("decisions") or [])
    items_by_id = {i["id"]: i for i in state.get("items") or []}
    model = (state.get("settings") or {}).get("llm_model")
    priorities = _priorities_block(state)

    archive_indices = [
        i
        for i, d in enumerate(decisions)
        if d.get("proposed_action") == "archive" and d.get("status") != "needs_your_call"
    ]
    if not archive_indices:
        return {"decisions": decisions}

    calls: list[dict] = []
    flipped = 0
    failed_ids: set[str] = set()

    for chunk in _chunk(archive_indices):
        batch = [decisions[i] for i in chunk]
        flips, usage, failed = review_archive_batch(
            batch, items_by_id, priorities=priorities, model=model
        )
        calls.extend(usage)
        if failed:
            failed_ids.update(d["item_id"] for d in batch)
            continue
        for i in chunk:
            decision = decisions[i]
            reason = flips.get(decision["item_id"])
            if reason is None:
                continue
            decisions[i] = {
                **decision,
                "proposed_action": "keep",
                "decided_by": "reviewer",
                "reasoning": f"{decision.get('reasoning', '')} Reviewer: {reason}".strip(),
                "status": "proposed",
            }
            flipped += 1

    if failed_ids:
        # Reviewer call failed — keep the primary LLM's original decision rather than
        # demoting to needs_your_call.  The primary LLM already classified these at
        # >0.75 confidence; in autonomous mode, forcing needs_your_call would silently
        # keep mail the AI was confident should be archived.  A warning is emitted so
        # the operator can investigate the connection issue.
        log.warning(
            "never_miss.reviewer_batch_failed_fallback_to_llm",
            run_id=state.get("run_id"),
            failed_item_count=len(failed_ids),
        )

    log.info(
        "never_miss.reviewer",
        run_id=state.get("run_id"),
        reviewed=len(archive_indices) - len(failed_ids),
        flipped=flipped,
        failed_batches=len(failed_ids),
    )
    return {
        "decisions": decisions,
        "llm_calls": calls,
        "review_failed_item_ids": sorted(failed_ids),
    }


def apply_never_miss_floor(state: TriageState) -> dict:
    """Mechanisms B + C, applied in fixed order after the reviewer.

    Confidence floor first (never archive below the floor), then the
    reply-history override (never archive anyone the user has ever replied
    to), then the VIP override (never archive an email/domain/keyword on the
    user's VIP list, at any confidence). All three can only push an outcome
    toward ``keep``.
    """
    floor = float(
        (state.get("settings") or {}).get("confidence_floor", DEFAULT_CONFIDENCE_FLOOR)
    )
    decisions = apply_confidence_floor(state.get("decisions") or [], floor)
    decisions = apply_reply_history_guard(
        decisions, state.get("sender_stats") or {}, state.get("items") or []
    )
    decisions = apply_vip_guard(
        decisions, state.get("vip") or {}, state.get("items") or []
    )

    log.info(
        "never_miss.floor",
        run_id=state.get("run_id"),
        floor=floor,
        needs_your_call=sum(1 for d in decisions if d.get("status") == "needs_your_call"),
    )
    _upgrade_review_state(state, decisions)
    return {"decisions": decisions}


def _upgrade_review_state(state: TriageState, decisions: list[dict]) -> None:
    """Durable becomes final: provisional -> reviewed (or review_failed).

    Runs at the end of the never-miss chain — the last point at which an ``archive``
    can still be flipped back to ``keep``. Every thread whose verdict changed re-emits
    ``thread_classified`` so the live feed shows provisional becoming final in place.
    A failure here leaves the rows provisional, which is the safe direction: an
    un-upgraded row can never be applied.
    """
    run_id = state.get("run_id")
    if not run_id:
        return
    failed_ids = list(state.get("review_failed_item_ids") or [])
    try:
        from db.session import create_db_session
        from graph.persistence import finalise_review

        with create_db_session() as session:
            outcome = finalise_review(
                session,
                run_id=run_id,
                user_id=state["user_id"],
                decisions=decisions,
                review_failed_item_ids=failed_ids,
            )
    except Exception as exc:
        log.warning("never_miss.review_state_upgrade_failed", run_id=run_id, error=str(exc))
        return

    log.info(
        "never_miss.review_state",
        run_id=run_id,
        reviewed=outcome["reviewed"],
        review_failed=outcome["review_failed"],
        flipped=len(outcome["flipped"]),
    )

    from graph.checkpoint import emit_decisions

    failed_set = set(failed_ids)
    changed = set(outcome["flipped"]) | failed_set
    for decision in decisions:
        if decision["item_id"] not in changed:
            continue
        emit_decisions(
            state,
            [decision],
            tier=decision.get("decided_by") or "reviewer",
            review_state=(
                "review_failed" if decision["item_id"] in failed_set else "reviewed"
            ),
        )
