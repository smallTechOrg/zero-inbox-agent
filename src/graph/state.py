from __future__ import annotations

import operator
from typing import Annotated, TypedDict


def keep_first_error(current: str | None, incoming: str | None) -> str | None:
    """Reducer for the ``error`` channel. Keeps the FIRST real error.

    ``error`` is a hard gate: any truthy value routes the graph to
    ``handle_error`` and ends the run. Without a reducer it is a LastValue
    channel, so when several tier-3 batches fan out via ``Send`` and finish in
    the SAME superstep — each returning ``{"error": ...}``, success or failure —
    LangGraph raises ``InvalidUpdateError: At key 'error': Can receive only one
    value per step`` and the whole run dies. That is what broke the 220-thread
    full-resume path while fixture-scale runs (one batch, one write per step)
    passed: the bug only appears once there are enough threads to produce
    concurrent batches.

    ``current or incoming`` is deliberate in both directions:
      * a concurrent SUCCESS (``None``) must never erase a sibling's real error,
        or a fatal batch would be silently swallowed and the run would carry on;
      * the first error wins over later ones, so the reported cause is the one
        that actually stopped things rather than whichever landed last.
    """
    return current or incoming


class TriageState(TypedDict, total=False):
    """State of one cost-tiered triage run. See spec/agent.md."""

    # identity / scope
    run_id: str
    user_id: str
    channel_account_id: str
    limit: int
    dry_run: bool
    fetch_after: str | None  # ISO timestamp; only threads newer than this are fetched

    # per-user context, loaded once
    categories: list[dict]
    rules: list[dict]
    sender_stats: dict[str, dict]
    settings: dict
    vip: dict                        # {emails: [], domains: [], keywords: []}
    priorities_profile: str

    # working set
    items: list[dict]
    resolved: Annotated[list[dict], operator.add]
    llm_queue: list[dict]
    deep_queue: Annotated[list[dict], operator.add]
    batches: list[list[dict]]
    batch: list[dict]
    llm_decisions: Annotated[list[dict], operator.add]
    llm_calls: Annotated[list[dict], operator.add]

    # outputs
    decisions: list[dict]
    clusters: list[dict]
    counts: dict
    cost: dict

    # resume (Phase 6, spec/capabilities/durable-resumable-runs.md)
    #: item ids (both persisted ids and channel thread ids) this run has already
    #: decided — filtered out of every tier queue so they are never re-classified.
    already_decided_item_ids: list[str]
    #: how many threads the interrupted leg(s) already decided (progress denominator)
    already_decided_count: int
    #: item ids whose reviewer pass could not complete — never applied
    review_failed_item_ids: list[str]

    # control
    error: Annotated[str | None, keep_first_error]
    status: str
