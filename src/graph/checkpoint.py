"""Incremental, durable persistence of triage decisions (Phase 6, slice 1).

Every tier of the cascade calls :func:`record_batch` the moment it resolves a batch.
The rows land in ``decisions`` with ``review_state="provisional"`` — **durable, not
final**. Only the second-pass reviewer may upgrade them to ``reviewed``, and only a
``reviewed`` row is ever eligible for a Gmail mutation
(``tools.actions.apply_decision`` raises ``NotReviewedError`` otherwise).

See spec/capabilities/durable-resumable-runs.md (Rule A). Before this module the whole
run's work lived in LangGraph's in-memory state until ``persist_decisions`` fired at the
very end of the graph, so an interrupted run lost every decision it had made.

A checkpoint failure is logged at WARNING and **never** kills the run: the un-checkpointed
threads are simply re-decided on resume, which the ``(run_id, item_id)`` unique
constraint makes safe.
"""

from __future__ import annotations

from graph.state import TriageState
from observability.events import get_logger

log = get_logger("triage")

#: state["decided_by"] -> the tier label carried by the SSE feed.
TIER_LABELS = ("rule", "sender_history", "llm", "llm_deep", "reviewer", "error")


def _emit_thread_classified(user_id: str, **payload) -> None:
    """Slice 2 owns ``events.bus.emit_thread_classified``; import defensively.

    Truncation and redaction happen inside the helper (spec pinned contract), so
    nothing here needs to trim — but nothing here may fail either.
    """
    try:
        from events.bus import emit_thread_classified
    except (ImportError, AttributeError):  # pragma: no cover - slice-2 not landed
        return
    try:
        emit_thread_classified(user_id, **payload)
    except Exception as exc:  # pragma: no cover - the bus must never fail a run
        log.warning("triage.thread_classified_emit_failed", error=str(exc))


def emit_decisions(
    state: TriageState,
    decisions: list[dict],
    *,
    tier: str,
    review_state: str,
    items: list[dict] | None = None,
) -> None:
    """One ``thread_classified`` event per decision — the coverage guarantee."""
    items_by_id = {i["id"]: i for i in (state.get("items") or [])}
    items_by_id.update({i["id"]: i for i in (items or [])})
    for decision in decisions:
        item = items_by_id.get(decision["item_id"], {})
        _emit_thread_classified(
            state["user_id"],
            run_id=state.get("run_id") or "",
            item_id=str(decision["item_id"]),
            subject=str(item.get("subject") or ""),
            from_email=str(item.get("from_email") or ""),
            category=str(decision.get("category") or ""),
            action=str(decision.get("proposed_action") or ""),
            decided_by=str(decision.get("decided_by") or tier),
            confidence=float(decision.get("confidence") or 0.0),
            reasoning=str(decision.get("reasoning") or ""),
            review_state=review_state,
        )


def record_batch(
    state: TriageState,
    decisions: list[dict],
    *,
    tier: str,
    llm_calls: list[dict] | None = None,
    items: list[dict] | None = None,
) -> int:
    """Persist one tier's batch in its own short transaction. Returns rows written.

    Upserts the batch's items, inserts the decisions as ``provisional`` (skipping any
    ``(run_id, item_id)`` that already exists), appends the batch's ``llm_calls`` rows,
    bumps ``triage_runs.items_decided`` atomically by the number actually inserted, and
    emits one ``thread_classified`` event per decision.
    """
    if not decisions:
        return 0
    run_id = state.get("run_id")
    written = 0
    if run_id:
        try:
            from db.session import create_db_session
            from graph.persistence import insert_provisional_decisions

            wanted = {d["item_id"] for d in decisions}
            by_id = {i["id"]: i for i in (state.get("items") or [])}
            by_id.update({i["id"]: i for i in (items or [])})
            batch_items = [by_id[i] for i in wanted if i in by_id]
            with create_db_session() as session:
                written = insert_provisional_decisions(
                    session,
                    run_id=run_id,
                    user_id=state["user_id"],
                    channel_account_id=state.get("channel_account_id") or "",
                    items=batch_items,
                    decisions=decisions,
                    llm_calls=llm_calls or [],
                )
            # Mark the spend as already persisted so the end-of-graph finalisation
            # does not write these rows a second time and double the run's cost.
            for call in llm_calls or []:
                call["_checkpointed"] = True
            _bump_decided(run_id, written)
            log.info(
                "triage.checkpoint",
                run_id=run_id,
                tier=tier,
                batch=len(decisions),
                written=written,
            )
        except Exception as exc:  # a checkpoint failure must never kill the run
            log.warning(
                "triage.checkpoint_failed", run_id=run_id, tier=tier, error=str(exc)
            )

    emit_decisions(state, decisions, tier=tier, review_state="provisional", items=items)
    return written


def _bump_decided(run_id: str, delta: int) -> None:
    """Atomic column-expression increment — parallel batches contend on this row."""
    if delta <= 0:
        return
    try:
        from sqlalchemy import update as sa_update

        from db.session import create_db_session
        from graph.persistence import model_for

        run_cls = model_for("triage_runs")
        if run_cls is None:
            return
        with create_db_session() as session:
            session.execute(
                sa_update(run_cls)
                .where(run_cls.id == run_id)
                .values(items_decided=run_cls.items_decided + delta)
            )
    except Exception as exc:  # pragma: no cover - progress must never fail a run
        log.warning("triage.checkpoint_progress_failed", run_id=run_id, error=str(exc))
