"""Triage graph assembly. Structure is specified in spec/agent.md.

Phase 1 has no ``second_pass_reviewer`` / ``apply_never_miss_floor`` node — those are
wired in Phase 2. Phase 1 is dry-run, so no mutation depends on them; the simple
confidence floor is applied before clustering and again before persistence.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from graph import edges, nodes
from graph.state import TriageState

MAX_CONCURRENCY = 4

_NODES = (
    "load_context",
    "fetch_items",
    "redact_items",
    "apply_deterministic_rules",
    "apply_sender_history",
    "prepare_llm_batches",
    "llm_classify_batch",
    "deep_read_escalation",
    "cluster_decisions",
    "persist_decisions",
    "handle_error",
    "finalize",
)


def _fan_out(state: TriageState):
    return [
        Send("llm_classify_batch", {**state, "batch": batch})
        for batch in (state.get("batches") or [])
    ]


def build_triage_graph():
    g = StateGraph(TriageState)
    for name in _NODES:
        g.add_node(name, getattr(nodes, name))

    g.add_edge(START, "load_context")
    g.add_conditional_edges(
        "load_context",
        edges.guard("fetch_items"),
        {"fetch_items": "fetch_items", "handle_error": "handle_error"},
    )
    g.add_conditional_edges(
        "fetch_items",
        edges.guard("redact_items"),
        {"redact_items": "redact_items", "handle_error": "handle_error"},
    )
    g.add_edge("redact_items", "apply_deterministic_rules")
    g.add_edge("apply_deterministic_rules", "apply_sender_history")
    g.add_conditional_edges(
        "apply_sender_history",
        edges.route_after_history,
        {
            "llm": "prepare_llm_batches",
            "skip_llm": "cluster_decisions",
            "handle_error": "handle_error",
        },
    )
    g.add_conditional_edges("prepare_llm_batches", _fan_out, ["llm_classify_batch"])
    g.add_conditional_edges(
        "llm_classify_batch",
        edges.route_after_llm,
        {
            "deep": "deep_read_escalation",
            "done": "cluster_decisions",
            "handle_error": "handle_error",
        },
    )
    g.add_edge("deep_read_escalation", "cluster_decisions")
    g.add_conditional_edges(
        "cluster_decisions",
        edges.guard("persist_decisions"),
        {"persist_decisions": "persist_decisions", "handle_error": "handle_error"},
    )
    g.add_conditional_edges(
        "persist_decisions",
        edges.guard("finalize"),
        {"finalize": "finalize", "handle_error": "handle_error"},
    )
    g.add_edge("finalize", END)
    g.add_edge("handle_error", END)
    return g.compile()


triage_graph = build_triage_graph()
