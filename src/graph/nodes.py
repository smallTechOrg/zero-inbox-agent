"""Triage graph nodes (spec/agent.md § Nodes).

All external effects go through :class:`TriageDeps` — injected callables wired
to the real DB / Gmail / LLM / event-bus layers by ``graph.runner`` and to
fakes in unit tests. Node functions are pure state → state-update logic plus
those calls; every node is wrapped so an exception becomes the ``error``
channel, never a traceback.

Ordering invariants (binding):
- a decision row is persisted BEFORE its feed event and BEFORE any mutation;
- the audit row for a mutation is written by the Gmail choke point BEFORE the
  Gmail call executes (``apply_mutation`` contract);
- events are emitted best-effort — an event-bus problem never kills a run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Callable, Sequence

from graph.state import (
    CONFIDENCE_REVIEW_THRESHOLD,
    INVALID_OUTPUT_REASON,
    ClassifierView,
    ClassifyOutcome,
    CostTotals,
    Decision,
    RunState,
    TaxonomyEntry,
    label_for,
    view_from_mapping,
)

_log = logging.getLogger("zero_inbox.graph")


# --------------------------------------------------------------------- deps


@dataclass(slots=True)
class TriageDeps:
    """Injected effects. Signatures are the cross-slice contracts (spec/roadmap.md)."""

    #: (user_id, limit) -> newest-first INBOX threads as ClassifierViews/mappings
    fetch_inbox_threads: Callable[[str, int], Sequence[Any]]
    #: (user_id) -> gmail_thread_ids already decided and not undone (never-redo)
    decided_thread_ids: Callable[[str], set[str]]
    #: (user_id) -> decided-but-unapplied decisions (decision row, no audit row)
    pending_decisions: Callable[[str], list[Decision]]
    #: (user_id) -> current categories, fresh each run (the agent adapts to edits)
    load_taxonomy: Callable[[str], list[TaxonomyEntry]]
    #: (user_id, run_id, decision) — persist the thread_decisions row NOW
    save_decision: Callable[[str, str, Decision], None]
    #: (views, taxonomy) -> ClassifyOutcome — ONE batched LLM call (src/llm)
    classify: Callable[[Sequence[ClassifierView], Sequence[TaxonomyEntry]], ClassifyOutcome]
    #: (user_id, run_id, thread_id, action, label_name, reason) -> bool applied.
    #: Contract: writes the audit row BEFORE calling Gmail; honors the
    #: test-isolation guard; returns False when the mutation already exists
    #: (idempotent resume) — src/channels/gmail/mutations choke point.
    apply_mutation: Callable[..., bool]
    #: (user_id, run_id, type, sentence, detail) — persist run_events row then emit
    publish_event: Callable[..., None]
    #: (run_id, **fields) — update the runs row
    update_run: Callable[..., None]
    #: LLM batch size (≤25 per spec)
    batch_size: int = 25


# ------------------------------------------------------------------ helpers


def _emit(deps: TriageDeps, state: RunState, type_: str, sentence: str,
          detail: dict[str, Any] | None = None) -> None:
    """Feed events are best-effort: never let the bus kill a run."""
    try:
        deps.publish_event(
            state["user_id"], state["run_id"], type_, sentence, detail or {}
        )
    except Exception:  # pragma: no cover - defensive
        _log.warning("event emit failed type=%s run=%s", type_, state.get("run_id"),
                     exc_info=True)


def humanize_error(exc: BaseException) -> str:
    """Map any exception to one human-actionable sentence. Never a traceback."""
    text = f"{type(exc).__name__} {exc}".lower()
    if "429" in text or "rate limit" in text or "ratelimit" in text or "rate-limit" in text:
        return ("The AI provider rate-limited us — the run paused safely. "
                "Press Clean my inbox to resume where it left off.")
    if any(k in text for k in ("invalid_grant", "reconnect", "credential", "refresh token",
                               "unauthorized", "unauthenticated", "401", "403")):
        return "Reconnect Gmail to continue."
    if "timeout" in text or "timed out" in text:
        return ("The AI provider timed out — the run paused safely. "
                "Press Clean my inbox to resume where it left off.")
    return ("Something went wrong mid-run — the run paused safely. "
            "Press Clean my inbox to resume where it left off.")


def _short(subject: str, limit: int = 60) -> str:
    subject = subject or "(no subject)"
    return subject if len(subject) <= limit else subject[: limit - 1] + "…"


def _needs_review_category(taxonomy: Sequence[TaxonomyEntry]) -> TaxonomyEntry:
    for entry in taxonomy:
        if entry.is_needs_review:
            return entry
    # The reserved category is seeded per user and not deletable; degrade loudly.
    raise RuntimeError("taxonomy has no 'Needs review' category — reseed categories")


def _decision_from_result(result: dict[str, Any], view: ClassifierView,
                          taxonomy: Sequence[TaxonomyEntry]) -> Decision:
    by_name = {t.name.strip().lower(): t for t in taxonomy}
    raw_name = str(result.get("category", "")).strip()
    entry = by_name.get(raw_name.lower())
    try:
        confidence = max(0.0, min(1.0, float(result.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    reason = str(result.get("reason", "")).strip() or "no reason given"
    if entry is None:
        # Unknown category name from the model → best guess is "Needs review".
        entry = _needs_review_category(taxonomy)
        reason = f"{INVALID_OUTPUT_REASON}: unknown category {raw_name!r}"
        confidence = 0.0
    needs_review = confidence < CONFIDENCE_REVIEW_THRESHOLD
    return Decision(
        thread_id=view.thread_id,
        category_id=entry.id,
        category_name=entry.name,
        confidence=confidence,
        reason=reason,
        needs_review=needs_review,
        subject=view.subject,
        sender=view.sender_address,
    )


def _invalid_decision(view: ClassifierView, taxonomy: Sequence[TaxonomyEntry]) -> Decision:
    entry = _needs_review_category(taxonomy)
    return Decision(
        thread_id=view.thread_id,
        category_id=entry.id,
        category_name=entry.name,
        confidence=0.0,
        reason=INVALID_OUTPUT_REASON,
        needs_review=True,
        subject=view.subject,
        sender=view.sender_address,
    )


# -------------------------------------------------------------------- nodes


def make_nodes(deps: TriageDeps) -> dict[str, Callable[[RunState], dict[str, Any]]]:
    """Build the node functions bound to *deps*."""

    def load_chunk(state: RunState) -> dict[str, Any]:
        user_id = state["user_id"]
        limit = int(state.get("chunk_limit", 50))
        decided = deps.decided_thread_ids(user_id)
        raw = deps.fetch_inbox_threads(user_id, limit)
        views: list[ClassifierView] = []
        for item in raw:
            view = item if isinstance(item, ClassifierView) else view_from_mapping(item)
            if view.thread_id not in decided:
                views.append(view)
        views = views[:limit]
        batch = max(1, int(deps.batch_size))
        batches = [views[i : i + batch] for i in range(0, len(views), batch)]
        # Resumability: decisions persisted by a dead run but never applied
        # (no audit row) are re-applied idempotently before new work.
        pending = deps.pending_decisions(user_id)
        if views or pending:
            sentence = f"Loaded {len(views)} inbox threads to triage, newest first."
            if pending:
                sentence += f" Resuming {len(pending)} unapplied decisions from the last run."
        else:
            sentence = "Inbox chunk clean — nothing new to triage."
        update: dict[str, Any] = {
            "threads": views,
            "batches": batches,
            "batch_index": 0,
            "decisions": list(pending),
            "applied_count": 0,
        }
        _emit(deps, state, "chunk_loaded", sentence,
              {"threads": len(views), "pending": len(pending), "batches": len(batches)})
        return update

    def match_profiles(state: RunState) -> dict[str, Any]:
        # Phase 1 pass-through; Phase 2 decides threads via sender_profiles here.
        return {}

    def classify_batch(state: RunState) -> dict[str, Any]:
        batches = state.get("batches", [])
        index = state.get("batch_index", 0)
        if index >= len(batches):
            return {"batch_index": index}
        views = batches[index]
        taxonomy = deps.load_taxonomy(state["user_id"])  # fresh each call
        outcome = deps.classify(views, taxonomy)

        by_id = {str(r.get("thread_id", "")): r for r in outcome.results
                 if isinstance(r, dict)}
        decisions = list(state.get("decisions", []))
        counts = dict(state.get("counts", {}))
        for view in views:
            result = by_id.get(view.thread_id)
            if result is None:
                decision = _invalid_decision(view, taxonomy)
            else:
                decision = _decision_from_result(result, view, taxonomy)
            # Persist FIRST — the decision index is the resume source of truth.
            deps.save_decision(state["user_id"], state["run_id"], decision)
            decisions.append(decision)
            counts[decision.category_name] = counts.get(decision.category_name, 0) + 1
            sentence = (
                f"Classifying '{_short(view.subject)}' from "
                f"{view.sender_address or 'unknown sender'} → {decision.category_name} "
                f"({decision.confidence:.0%})"
            )
            if decision.needs_review:
                sentence += " — flagged for review"
            _emit(deps, state, "decision", sentence, {
                "thread_id": decision.thread_id,
                "category": decision.category_name,
                "confidence": decision.confidence,
                "reason": decision.reason,
                "needs_review": decision.needs_review,
            })

        cost = state.get("cost") or CostTotals()
        cost = replace(
            cost,
            calls=cost.calls + 1,
            tokens_in=cost.tokens_in + outcome.tokens_in,
            tokens_out=cost.tokens_out + outcome.tokens_out,
            est_cost_usd=round(cost.est_cost_usd + outcome.est_cost_usd, 6),
            fallback_events=cost.fallback_events + (1 if outcome.was_fallback else 0),
        )
        if outcome.was_fallback:
            _emit(deps, state, "fallback",
                  outcome.fallback_reason
                  or "NVIDIA was unavailable — switched to Gemini for this batch.",
                  {"provider": outcome.provider, "model": outcome.model})
        _emit(deps, state, "cost_tick",
              f"LLM calls: {cost.calls} · tokens {cost.tokens_in}/{cost.tokens_out} · "
              f"est ${cost.est_cost_usd:.4f}",
              {**cost.as_dict(), "provider": outcome.provider, "model": outcome.model,
               "latency_ms": outcome.latency_ms})
        return {
            "decisions": decisions,
            "counts": counts,
            "cost": cost,
            "batch_index": index + 1,
        }

    def apply_actions(state: RunState) -> dict[str, Any]:
        taxonomy = deps.load_taxonomy(state["user_id"])
        rules = {t.id: t for t in taxonomy}
        decisions = state.get("decisions", [])
        start = state.get("applied_count", 0)
        for decision in decisions[start:]:
            entry = rules.get(decision.category_id)
            rule = entry.rule if entry else "label_only"
            archived = rule == "label_and_archive"
            detail = {
                "thread_id": decision.thread_id,
                "category": decision.category_name,
                "archived": archived,
                "needs_review": decision.needs_review,
                "reason": decision.reason,
            }
            # Exactly ONE feed event per mutation actually applied
            # (spec/capabilities/live-activity-feed.md) — a resume that skips
            # an already-audited mutation emits nothing for it.
            # Always: the category label. The choke point audits before Gmail.
            if deps.apply_mutation(
                state["user_id"], state["run_id"], decision.thread_id,
                "add_label", label_for(decision.category_name), decision.reason,
            ):
                sentence = f"Filed '{_short(decision.subject)}' → {decision.category_name}"
                if archived:
                    sentence += " — archiving"
                if decision.needs_review:
                    sentence += " — needs review"
                _emit(deps, state, "action", sentence, detail)
            if archived and deps.apply_mutation(
                state["user_id"], state["run_id"], decision.thread_id,
                "remove_inbox", None, decision.reason,
            ):
                _emit(deps, state, "action",
                      f"Archived '{_short(decision.subject)}'", detail)
            if decision.needs_review and not (entry and entry.is_needs_review):
                needs_review = _needs_review_category(taxonomy)
                if deps.apply_mutation(
                    state["user_id"], state["run_id"], decision.thread_id,
                    "add_label", label_for(needs_review.name),
                    f"low confidence ({decision.confidence:.0%})",
                ):
                    _emit(deps, state, "action",
                          f"Flagged '{_short(decision.subject)}' for review "
                          f"({decision.confidence:.0%} confidence)", detail)
        return {"applied_count": len(decisions)}

    def error_handler(state: RunState) -> dict[str, Any]:
        reason = state.get("error") or "The run stopped unexpectedly."
        try:
            deps.update_run(state["run_id"], status="interrupted",
                            interrupt_reason=reason)
        except Exception:  # pragma: no cover - the feed event must still go out
            _log.warning("could not persist interrupted status run=%s",
                         state.get("run_id"), exc_info=True)
        _emit(deps, state, "run_interrupted", reason, {"reason": reason})
        return {}

    def finalize(state: RunState) -> dict[str, Any]:
        decisions = state.get("decisions", [])
        counts = state.get("counts", {})
        cost = state.get("cost") or CostTotals()
        interrupted = bool(state.get("error"))
        try:
            deps.update_run(
                state["run_id"],
                status="interrupted" if interrupted else "completed",
                threads_decided=len(decisions),
                counts_json=counts,
                llm_calls=cost.calls,
                tokens_in=cost.tokens_in,
                tokens_out=cost.tokens_out,
                est_cost_usd=cost.est_cost_usd,
                fallback_events=cost.fallback_events,
            )
        except Exception:
            _log.warning("could not persist run totals run=%s",
                         state.get("run_id"), exc_info=True)
        if interrupted:
            sentence = (f"Run paused after {len(decisions)} threads — "
                        f"{state.get('error')}")
        elif not decisions:
            sentence = "Inbox chunk clean — nothing needed doing."
        else:
            breakdown = ", ".join(f"{n} {name}" for name, n in sorted(counts.items()))
            sentence = (f"Cleaned {len(decisions)} threads: {breakdown} "
                        f"(est ${cost.est_cost_usd:.4f}).")
        _emit(deps, state, "run_finished", sentence, {
            "status": "interrupted" if interrupted else "completed",
            "threads_decided": len(decisions),
            "counts": counts,
            **cost.as_dict(),
        })
        return {}

    return {
        "load_chunk": load_chunk,
        "match_profiles": match_profiles,
        "classify_batch": classify_batch,
        "apply_actions": apply_actions,
        "error_handler": error_handler,
        "finalize": finalize,
    }
