from __future__ import annotations

import operator
from typing import Annotated, TypedDict


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
    vip_entries: list[dict]
    priority_profile: str

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

    # control
    error: str | None
    status: str
