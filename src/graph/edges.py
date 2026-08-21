"""Routing predicates for the triage graph (spec/agent.md § Edges).

Every post-node route first checks the error channel: any node that trapped an
exception routes to ``error_handler``, which humanizes it and hands off to
``finalize``. No exception ever escapes the graph as a traceback.
"""

from __future__ import annotations

from graph.state import RunState

ERROR = "error_handler"


def has_threads(state: RunState) -> bool:
    """Anything to do this run? New threads OR recovered unapplied decisions."""
    return bool(state.get("threads")) or bool(state.get("decisions"))


def more_batches(state: RunState) -> bool:
    return state.get("batch_index", 0) < len(state.get("batches", []))


def route_after_load(state: RunState) -> str:
    if state.get("error"):
        return ERROR
    return "match_profiles" if has_threads(state) else "finalize"


def route_after_match(state: RunState) -> str:
    return ERROR if state.get("error") else "classify_batch"


def route_after_classify(state: RunState) -> str:
    return ERROR if state.get("error") else "apply_actions"


def route_after_apply(state: RunState) -> str:
    if state.get("error"):
        return ERROR
    return "classify_batch" if more_batches(state) else "finalize"
