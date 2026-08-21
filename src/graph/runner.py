"""Run entry point + default dependency wiring for the triage graph.

``run_triage(user_id, run_id)`` is what the API starts as a background task
(spec/agent.md § Assembly). All cross-slice effects live here as small wiring
functions so the graph itself stays pure and unit-testable; each wiring
function imports its wave-1 module lazily and targets the contract named in
spec/roadmap.md Phase-1 slices:

- ``db.models`` (db-and-domain): ``Run``, ``ThreadDecision``, ``Mutation``,
  ``Category`` classes with the spec/data.md columns.
- ``channels.gmail.client.list_inbox_threads(user_id, limit)`` (auth-gmail):
  newest-first INBOX threads, metadata-format, as mappings/ClassifierViews.
- ``channels.gmail.mutations.execute_mutation(db, user_id=, run_id=,
  gmail_thread_id=, action=, label_name=, reason=)`` (auth-gmail): the choke
  point — audit row first, reversible ops only, test-isolation guard.
- ``events.publish_run_event(db, user_id=, run_id=, type=, sentence=,
  detail=)`` (runs-undo-api): persist the run_events row, then emit on the bus.
- ``llm.get_llm_client().classify_batch(...)`` (llm-provider): one batched
  call, NVIDIA → Gemini fallback inside, hard timeout, cost accounting.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Sequence

from graph.agent import build_agent
from graph.nodes import TriageDeps
from graph.state import (
    ClassifierView,
    ClassifyOutcome,
    CostTotals,
    Decision,
    RunState,
    TaxonomyEntry,
    view_from_mapping,
)

_log = logging.getLogger("zero_inbox.graph")

_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "classify.md"

#: JSON Schema for ONE classification result — the strict output contract.
RESULT_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["thread_id", "category", "confidence", "reason"],
    "properties": {
        "thread_id": {"type": "string"},
        "category": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string", "maxLength": 300},
    },
    "additionalProperties": False,
}


def load_classify_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def build_instructions(taxonomy: Sequence[TaxonomyEntry]) -> str:
    """The classify prompt + the user's CURRENT taxonomy (fresh each run)."""
    lines = [
        f"- {t.name}: {t.description or '(no description)'}" for t in taxonomy
    ]
    return (
        load_classify_prompt()
        + "\n\nCATEGORIES (choose exactly one `category` per thread, verbatim "
        "from this list):\n"
        + "\n".join(lines)
    )


# ------------------------------------------------------- default dep wiring


def _fetch_inbox_threads(user_id: str, limit: int) -> list[ClassifierView]:
    from channels.gmail.client import list_inbox_threads  # auth-gmail contract

    return [
        v if isinstance(v, ClassifierView) else view_from_mapping(v)
        for v in list_inbox_threads(user_id, limit=limit)
    ]


def _decided_thread_ids(user_id: str) -> set[str]:
    from db.models import ThreadDecision
    from db.session import create_db_session

    with create_db_session() as db:
        rows = (
            db.query(ThreadDecision.gmail_thread_id)
            .filter(
                ThreadDecision.user_id == user_id,
                ThreadDecision.undone == False,  # noqa: E712 — ORM expression
            )
            .all()
        )
        return {r[0] for r in rows}


def _pending_decisions(user_id: str) -> list[Decision]:
    """Decision rows with no audit rows: decided by a dead run, never applied."""
    from db.models import Category, Mutation, ThreadDecision
    from db.session import create_db_session

    with create_db_session() as db:
        mutated = {
            r[0]
            for r in db.query(Mutation.gmail_thread_id)
            .filter(Mutation.user_id == user_id, Mutation.undone_at.is_(None))
            .all()
        }
        rows = (
            db.query(ThreadDecision, Category.name)
            .join(Category, ThreadDecision.category_id == Category.id)
            .filter(
                ThreadDecision.user_id == user_id,
                ThreadDecision.undone == False,  # noqa: E712
            )
            .all()
        )
        return [
            Decision(
                thread_id=d.gmail_thread_id,
                category_id=d.category_id,
                category_name=name,
                confidence=d.confidence,
                reason=d.reason,
                needs_review=d.needs_review,
                subject=d.subject,
                sender=d.sender,
            )
            for d, name in rows
            if d.gmail_thread_id not in mutated
        ]


def _load_taxonomy(user_id: str) -> list[TaxonomyEntry]:
    from db.models import Category
    from db.session import create_db_session

    with create_db_session() as db:
        rows = (
            db.query(Category)
            .filter(Category.user_id == user_id)
            .order_by(Category.position)
            .all()
        )
        return [
            TaxonomyEntry(
                id=c.id,
                name=c.name,
                description=c.description or "",
                rule=c.rule,
                is_needs_review=bool(c.is_needs_review),
            )
            for c in rows
        ]


def _save_decision(user_id: str, run_id: str, decision: Decision) -> None:
    """Upsert on (user_id, gmail_thread_id): an UNDONE thread is re-decidable —
    its old row is overwritten by the new run's decision (never a dupe row)."""
    from db.models import ThreadDecision
    from db.session import create_db_session

    with create_db_session() as db:
        existing = (
            db.query(ThreadDecision)
            .filter(
                ThreadDecision.user_id == user_id,
                ThreadDecision.gmail_thread_id == decision.thread_id,
            )
            .one_or_none()
        )
        if existing is not None:
            existing.run_id = run_id
            existing.sender = decision.sender
            existing.subject = decision.subject
            existing.category_id = decision.category_id
            existing.confidence = decision.confidence
            existing.reason = decision.reason
            existing.needs_review = decision.needs_review
            existing.undone = False
            existing.source = "llm"
            return
        db.add(
            ThreadDecision(
                user_id=user_id,
                run_id=run_id,
                gmail_thread_id=decision.thread_id,
                sender=decision.sender,
                subject=decision.subject,
                snippet="",
                category_id=decision.category_id,
                confidence=decision.confidence,
                reason=decision.reason,
                needs_review=decision.needs_review,
                source="llm",
            )
        )


def _apply_mutation(user_id: str, run_id: str, gmail_thread_id: str,
                    action: str, label_name: str | None, reason: str) -> bool:
    """Idempotent: an already-audited, not-undone identical mutation is skipped."""
    from channels.gmail.mutations import execute_mutation  # audit-first choke point
    from db.models import Mutation
    from db.session import create_db_session

    with create_db_session() as db:
        exists = (
            db.query(Mutation.id)
            .filter(
                Mutation.user_id == user_id,
                Mutation.gmail_thread_id == gmail_thread_id,
                Mutation.action == action,
                Mutation.label_name == label_name,
                Mutation.undone_at.is_(None),
            )
            .first()
        )
        if exists:
            return False
        execute_mutation(
            db,
            user_id=user_id,
            run_id=run_id,
            gmail_thread_id=gmail_thread_id,
            action=action,
            label_name=label_name,
            reason=reason,
        )
        return True


def _publish_event(user_id: str, run_id: str, type_: str, sentence: str,
                   detail: dict[str, Any]) -> None:
    from db.session import create_db_session
    from events import publish_run_event  # runs-undo-api contract — must exist

    with create_db_session() as db:
        publish_run_event(db, user_id=user_id, run_id=run_id, type=type_,
                          sentence=sentence, detail=detail)


def _update_run(run_id: str, **fields: Any) -> None:
    from datetime import datetime, timezone

    from db.models import Run
    from db.session import create_db_session

    with create_db_session() as db:
        run = db.get(Run, run_id)
        if run is None:  # pragma: no cover - runs row is created by POST /runs
            _log.warning("run %s not found while updating %s", run_id, sorted(fields))
            return
        for key, value in fields.items():
            setattr(run, key, value)
        if fields.get("status") in ("completed", "interrupted"):
            run.finished_at = datetime.now(timezone.utc)


def _classify(views: Sequence[ClassifierView],
              taxonomy: Sequence[TaxonomyEntry]) -> ClassifyOutcome:
    """One batched LLM call via src/llm (NVIDIA → Gemini fallback + hard timeout
    live in that layer). Adapts BatchClassification → ClassifyOutcome."""
    from llm import get_llm_client

    client = get_llm_client()
    schema = json.loads(json.dumps(RESULT_ITEM_SCHEMA))
    schema["properties"]["category"]["enum"] = [t.name for t in taxonomy]
    started = time.perf_counter()
    batch = asyncio.run(
        client.classify_batch(
            list(views),
            instructions=build_instructions(taxonomy),
            item_schema=schema,
        )
    )
    usage = batch.usage
    fallback = batch.fallback_events[0] if batch.fallback_events else None
    return ClassifyOutcome(
        results=list(batch.results),
        missing_ids=list(batch.missing_ids)
        + [str(e.get("raw", {}).get("thread_id", "")) for e in batch.invalid
           if isinstance(e.get("raw"), dict)],
        provider=(getattr(usage, "provider", "") or "nvidia") if usage else "nvidia",
        model=(usage.model if usage else "") or "",
        tokens_in=(usage.tokens_in if usage else 0) or 0,
        tokens_out=(usage.tokens_out if usage else 0) or 0,
        est_cost_usd=float((usage.usd if usage else 0.0) or 0.0),
        latency_ms=(usage.latency_ms if usage else 0)
        or int((time.perf_counter() - started) * 1000),
        was_fallback=batch.used_fallback,
        fallback_reason=(
            f"{fallback.from_provider.upper()} unavailable ({fallback.reason}) — "
            f"switched to {fallback.to_provider.capitalize()} for this batch."
            if fallback else ""
        ),
    )


def _with_llm_ledger(deps: TriageDeps, *, user_id: str, run_id: str) -> TriageDeps:
    """Wrap ``deps.classify`` so each real LLM call writes an ``llm_calls`` row."""
    import dataclasses

    inner = deps.classify

    def classify(views: Sequence[ClassifierView],
                 taxonomy: Sequence[TaxonomyEntry]) -> ClassifyOutcome:
        outcome = inner(views, taxonomy)
        try:
            from db.models import LlmCall
            from db.session import create_db_session

            with create_db_session() as db:
                db.add(
                    LlmCall(
                        user_id=user_id,
                        run_id=run_id,
                        provider=outcome.provider,
                        model=outcome.model,
                        tokens_in=outcome.tokens_in,
                        tokens_out=outcome.tokens_out,
                        latency_ms=outcome.latency_ms,
                        est_cost_usd=outcome.est_cost_usd,
                        was_fallback=outcome.was_fallback,
                    )
                )
        except Exception:  # noqa: BLE001 — the ledger never fails a run
            _log.warning("llm_calls ledger write failed run=%s", run_id, exc_info=True)
        return outcome

    return dataclasses.replace(deps, classify=classify)


def default_deps() -> TriageDeps:
    from config.settings import get_settings

    return TriageDeps(
        fetch_inbox_threads=_fetch_inbox_threads,
        decided_thread_ids=_decided_thread_ids,
        pending_decisions=_pending_decisions,
        load_taxonomy=_load_taxonomy,
        save_decision=_save_decision,
        classify=_classify,
        apply_mutation=_apply_mutation,
        publish_event=_publish_event,
        update_run=_update_run,
        batch_size=get_settings().llm_batch_size,
    )


# ------------------------------------------------------------------- entry


def run_triage(user_id: str, run_id: str, *, chunk_limit: int = 50,
               deps: TriageDeps | None = None) -> dict[str, Any]:
    """Execute one cleaning chunk for *user_id* under run *run_id*.

    Synchronous by design — the API starts it as a FastAPI background task.
    Never raises for run-level failures: any error is humanized, persisted as
    an ``interrupted`` run and emitted as ``run_interrupted``; the next trigger
    resumes. Returns the run summary.
    """
    if deps is None:
        deps = default_deps()
        # Production wiring only: every real LLM call lands one llm_calls
        # ledger row (spec/data.md § llm_calls) tagged with this run.
        deps = _with_llm_ledger(deps, user_id=user_id, run_id=run_id)
    agent = build_agent(deps)
    initial: RunState = {
        "run_id": run_id,
        "user_id": user_id,
        "chunk_limit": chunk_limit,
        "threads": [],
        "batches": [],
        "batch_index": 0,
        "decisions": [],
        "applied_count": 0,
        "counts": {},
        "cost": CostTotals(),
        "error": None,
    }
    started = time.perf_counter()
    _log.info(json.dumps({"event": "triage_run_started", "run_id": run_id,
                          "user_id": user_id, "chunk_limit": chunk_limit}))
    final = agent.invoke(initial)
    cost = final.get("cost") or CostTotals()
    summary = {
        "run_id": run_id,
        "status": "interrupted" if final.get("error") else "completed",
        "threads_decided": len(final.get("decisions", [])),
        "counts": final.get("counts", {}),
        "cost": cost.as_dict(),
        "error": final.get("error"),
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }
    _log.info(json.dumps({"event": "triage_run_finished", **summary}))
    return summary
