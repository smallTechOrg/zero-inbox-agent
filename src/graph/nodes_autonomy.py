"""The two Phase 7 autonomy nodes — thin LangGraph wrappers over ``graph.autonomy``.

They bracket the never-miss chain (spec/agent.md § Edges):

    cluster_decisions -> align_to_category_default -> second_pass_reviewer
    ...
    apply_never_miss_floor -> mark_autonomy_state -> persist_decisions

That ordering *is* the safety argument. ``align_to_category_default`` is the only
stage in the whole system permitted to move a decision from ``keep`` toward
``archive``, and it runs before the reviewer, the confidence floor and the VIP /
reply-history guards — so every category-driven archive is audited by all of them
and any of them can still veto it. ``mark_autonomy_state`` runs at the *end* of
the chain, when every verdict is final, so nothing is stamped ``auto_act`` before
the reviewer has had its say.

Both nodes operate on ``state["decisions"]``, which carries no reducer (see
``graph/state.py``), so each node's return value is the authoritative snapshot
for the rest of the run.
"""

from __future__ import annotations

from graph import autonomy
from graph.state import TriageState
from observability.events import get_logger

log = get_logger("autonomy")


def _category_index(state: TriageState) -> dict[str, dict]:
    """Maps the category key a decision carries to its full category row.

    ``state["categories"]`` entries carry ``default_action`` and (Phase 7)
    ``auto_act_threshold`` — a pinned cross-slice contract on
    ``graph.persistence._load_context_rows``. A category row that predates that
    contract simply resolves ``auto_act_threshold`` to ``None``, i.e. inherit the
    global bar, which is the safe default.
    """
    index: dict[str, dict] = {}
    for category in state.get("categories") or []:
        key = category.get("key")
        if key:
            index[key] = category
    return index


def _items_by_id(state: TriageState) -> dict[str, dict]:
    return {item["id"]: item for item in (state.get("items") or []) if item.get("id")}


def align_to_category_default(state: TriageState) -> dict:
    """Rules C1-C5. Convert confident ``keep``s in archive-by-default categories.

    Never changes an ``archive`` to a ``keep``, never changes ``confidence`` or
    ``decided_by``, and never touches ``status`` or ``review_state`` (Rule C2).
    """
    try:
        settings = state.get("settings") or {}
        categories = _category_index(state)
        items = _items_by_id(state)
        sender_stats = state.get("sender_stats") or {}
        vip = state.get("vip") or {}

        aligned: list[dict] = []
        converted = 0
        for decision in state.get("decisions") or []:
            decision = dict(decision)
            category = categories.get(decision.get("category"))
            item = items.get(decision.get("item_id"))
            if autonomy.should_align(
                decision, category, settings, sender_stats, vip, item=item
            ):
                decision["proposed_action"] = category["default_action"]
                decision["reasoning"] = (
                    f"{decision.get('reasoning', '')} "
                    f"{autonomy.alignment_reasoning(decision, category, settings)}"
                ).strip()
                converted += 1
            aligned.append(decision)

        clusters = autonomy.refresh_cluster_actions(
            [dict(c) for c in (state.get("clusters") or [])], aligned
        )

        log.info(
            "triage.aligned_to_category_default",
            run_id=state.get("run_id"),
            user_id=state.get("user_id"),
            decisions=len(aligned),
            converted=converted,
        )
        return {"decisions": aligned, "clusters": clusters, "error": None}
    except Exception as exc:  # noqa: BLE001 - surfaced as state["error"] -> handle_error
        return {"error": f"align_to_category_default failed: {exc}"}


def mark_autonomy_state(state: TriageState) -> dict:
    """Stamp every decision with exactly one ``autonomy_state`` (fixed precedence).

    Read-only with respect to every other field: this node explains the outcome,
    it never changes it.
    """
    try:
        settings = state.get("settings") or {}
        categories = _category_index(state)
        items = _items_by_id(state)
        sender_stats = state.get("sender_stats") or {}
        vip = state.get("vip") or {}

        stamped: list[dict] = []
        tally: dict[str, int] = {}
        for decision in state.get("decisions") or []:
            decision = dict(decision)
            value = autonomy.classify_autonomy_state(
                decision,
                categories.get(decision.get("category")),
                settings,
                sender_stats,
                vip,
                item=items.get(decision.get("item_id")),
            )
            decision["autonomy_state"] = value
            tally[value] = tally.get(value, 0) + 1
            stamped.append(decision)

        log.info(
            "triage.autonomy_marked",
            run_id=state.get("run_id"),
            user_id=state.get("user_id"),
            decisions=len(stamped),
            **{f"n_{k}": v for k, v in tally.items()},
        )
        return {"decisions": stamped, "error": None}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"mark_autonomy_state failed: {exc}"}


__all__ = ["align_to_category_default", "mark_autonomy_state"]
