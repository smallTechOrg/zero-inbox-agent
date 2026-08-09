"""Triage graph assembly. Structure is specified in spec/agent.md.

Phase 2 wires in the never-miss safeguards from ``graph.nodes_review``:
``second_pass_reviewer`` (mechanism A, the false-negative-hunting LLM pass) and
``apply_never_miss_floor`` (mechanisms B + C, the confidence floor and the
reply-history override). Both operate on ``state["decisions"]`` — which carries
no reducer, so each node's return value is the authoritative snapshot for the
rest of the run — and run after ``cluster_decisions`` (which already merges
tiers 1-4 into that key) and before ``persist_decisions``, which re-applies the
floor once more as a final, idempotent safety net.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from graph import edges, nodes, nodes_review
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
    "second_pass_reviewer",
    "apply_never_miss_floor",
    "persist_decisions",
    "handle_error",
    "finalize",
)

_REVIEW_NODES = {
    "second_pass_reviewer": nodes_review.second_pass_reviewer,
    "apply_never_miss_floor": nodes_review.apply_never_miss_floor,
}


def _fan_out(state: TriageState):
    return [
        Send("llm_classify_batch", {**state, "batch": batch})
        for batch in (state.get("batches") or [])
    ]


def build_triage_graph():
    g = StateGraph(TriageState)
    for name in _NODES:
        g.add_node(name, _REVIEW_NODES.get(name) or getattr(nodes, name))

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
            "handle_error": "handle_error",
        },
    )
    g.add_edge("deep_read_escalation", "cluster_decisions")
    g.add_conditional_edges(
        "cluster_decisions",
        edges.guard("second_pass_reviewer"),
        {"second_pass_reviewer": "second_pass_reviewer", "handle_error": "handle_error"},
    )
    g.add_conditional_edges(
        "second_pass_reviewer",
        edges.guard("apply_never_miss_floor"),
        {"apply_never_miss_floor": "apply_never_miss_floor", "handle_error": "handle_error"},
    )
    g.add_conditional_edges(
        "apply_never_miss_floor",
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
