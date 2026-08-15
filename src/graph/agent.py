"""Graph assembly (spec/agent.md § Assembly).

Every worker node is wrapped so an exception lands in the ``error`` state
channel and routes to ``error_handler`` → ``finalize`` → END. ``finalize``
swallows its own failures — it must always run and always end the graph.
Resumability is data-driven (thread_decisions), so no checkpointer is used.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from langgraph.graph import END, StateGraph

from graph.edges import (
    route_after_apply,
    route_after_classify,
    route_after_load,
    route_after_match,
)
from graph.nodes import TriageDeps, humanize_error, make_nodes
from graph.state import RunState

_log = logging.getLogger("zero_inbox.graph")

_WORKER_NODES = ("load_chunk", "match_profiles", "classify_batch", "apply_actions")


def wrap_with_error_edge(fn: Callable[[RunState], dict[str, Any]]) -> Callable[[RunState], dict[str, Any]]:
    """Exceptions become the error channel — never a traceback out of the graph."""

    def inner(state: RunState) -> dict[str, Any]:
        if state.get("error"):
            return {}
        try:
            return fn(state)
        except Exception as exc:
            _log.warning("node %s failed: %s: %s", fn.__name__,
                         type(exc).__name__, exc, exc_info=True)
            return {"error": humanize_error(exc)}

    inner.__name__ = fn.__name__
    return inner


def build_agent(deps: TriageDeps):
    """Compile the triage graph bound to *deps*."""
    node_fns = make_nodes(deps)
    g = StateGraph(RunState)
    for name in _WORKER_NODES:
        g.add_node(name, wrap_with_error_edge(node_fns[name]))
    # error_handler/finalize are terminal-path nodes: they trap their own
    # failures internally and must never re-enter the error edge.
    g.add_node("error_handler", node_fns["error_handler"])
    g.add_node("finalize", node_fns["finalize"])

    g.set_entry_point("load_chunk")
    g.add_conditional_edges("load_chunk", route_after_load,
                            {"match_profiles": "match_profiles",
                             "finalize": "finalize",
                             "error_handler": "error_handler"})
    g.add_conditional_edges("match_profiles", route_after_match,
                            {"classify_batch": "classify_batch",
                             "error_handler": "error_handler"})
    g.add_conditional_edges("classify_batch", route_after_classify,
                            {"apply_actions": "apply_actions",
                             "error_handler": "error_handler"})
    g.add_conditional_edges("apply_actions", route_after_apply,
                            {"classify_batch": "classify_batch",
                             "finalize": "finalize",
                             "error_handler": "error_handler"})
    g.add_edge("error_handler", "finalize")
    g.add_edge("finalize", END)
    return g.compile()
