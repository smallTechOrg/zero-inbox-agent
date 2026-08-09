from __future__ import annotations

from collections.abc import Callable

from graph.state import TriageState


def guard(next_node: str) -> Callable[[TriageState], str]:
    """Route to ``next_node`` unless the state carries an error."""

    def _route(state: TriageState) -> str:
        return "handle_error" if state.get("error") else next_node

    return _route


def route_after_history(state: TriageState) -> str:
    """`"llm"` iff anything survived tiers 1-2, else skip the LLM entirely."""
    if state.get("error"):
        return "handle_error"
    return "llm" if state.get("llm_queue") else "skip_llm"


def route_after_llm(state: TriageState) -> str:
    """`"deep"` iff the LLM marked anything unsure."""
    if state.get("error"):
        return "handle_error"
    return "deep" if state.get("deep_queue") else "done"
