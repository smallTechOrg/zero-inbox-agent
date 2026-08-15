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

        pre_resolved = _preresolve_never_miss_for_review(
            aligned, categories, items, vip=vip, sender_stats=sender_stats
        )

        clusters = autonomy.refresh_cluster_actions(
            [dict(c) for c in (state.get("clusters") or [])], aligned
        )

        log.info(
            "triage.aligned_to_category_default",
            run_id=state.get("run_id"),
            user_id=state.get("user_id"),
            decisions=len(aligned),
            converted=converted,
            never_miss_pre_resolved=pre_resolved,
        )
        return {"decisions": aligned, "clusters": clusters, "error": None}
    except Exception as exc:  # noqa: BLE001 - surfaced as state["error"] -> handle_error
        return {"error": f"align_to_category_default failed: {exc}"}


def _preresolve_never_miss_for_review(
    decisions: list[dict],
    categories: dict[str, dict],
    items: dict[str, dict],
    *,
    vip: dict,
    sender_stats: dict,
) -> int:
    """Put every *deterministic* never-miss hold in front of the reviewer.

    Mutates ``decisions`` in place; returns how many were pre-resolved.

    **Why this exists, found by the real-model leg of the Phase 9 gate.** The
    reviewer audits ``proposed_action in REVIEWABLE_ACTIONS``, and only an
    audited row may ever become ``review_state="reviewed"`` (Phase 9, item zero).
    But a tier-2 reply-history hold is decided as a ``keep``, so before this it
    never entered a reviewer batch — and the reframe, which runs at the end of
    the chain, would then turn an *unaudited* row into an archive.
    ``NotReviewedError`` correctly refused it, and the six threads that verdict
    covered simply never left the inbox. Green tests, absent feature.

    The fix is not to relax the gate. It is to review the mail: a thread the
    never-miss layer is going to archive under a label is a **mutation**, so the
    reviewer must see it. This proposes the archive *before* the reviewer, which
    is the only point at which it can be audited, floored and vetoed.

    Nothing is weakened by doing so:

    * the reply-history and VIP guards run **after** the reviewer and still flip
      these rows straight back to ``keep`` — their veto is intact, and it is
      exactly that veto which produces the ``held_by_never_miss`` state the
      reframe then expresses as a label;
    * the confidence floor still binds first, so a below-floor thread becomes
      ``needs_your_call`` and is never archived;
    * a ``decided_by="rule"`` row is skipped — the user's own instruction
      outranks every guess about it (Rule C4);
    * only the deterministic triggers are pre-resolved. A reviewer flip cannot
      be, obviously — the reviewer has not run yet — but a flip *is* an audit, so
      those rows are covered by construction.
    """
    pre_resolved = 0
    for decision in decisions:
        if decision.get("proposed_action") in autonomy.LEAVING_ACTIONS:
            continue  # already reviewable
        if decision.get("status") == "needs_your_call":
            continue
        if decision.get("decided_by") in ("rule", "error"):
            continue

        item = items.get(decision.get("item_id"))
        deterministic = (
            autonomy.is_vip(item, vip)
            or autonomy.has_reply_history(item, sender_stats)
            or bool(decision.get("time_sensitive"))
        )
        if not deterministic:
            continue

        key = autonomy.never_miss_category_key(
            decision, item, vip=vip, sender_stats=sender_stats
        )
        if not key or key not in categories:
            continue

        decision["proposed_action"] = "archive"
        decision["category"] = key
        pre_resolved += 1
    return pre_resolved


def mark_autonomy_state(state: TriageState) -> dict:
    """Stamp every decision with exactly one ``autonomy_state``, then express the
    never-miss verdicts as labels (Phase 9).

    The stamping half is unchanged and still read-only with respect to every
    other field. The second half is the Phase 9 reframe: a decision stamped
    ``held_by_never_miss`` is no longer left sitting in the inbox — it is
    archived **under the label that names why it was held**. See
    :func:`_express_never_miss_as_label`.

    Ordering is the safety argument, and it is why the reframe lives here rather
    than anywhere earlier. This node runs at the very END of the never-miss chain
    (``align -> reviewer -> floor -> reply-history -> VIP``), when every verdict
    is final. Doing it earlier would put an archive in front of guards that only
    know how to convert ``archive -> keep``, and they would simply undo it.
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

        labelled, unresolvable = _express_never_miss_as_label(
            stamped, categories, items, vip=vip, sender_stats=sender_stats
        )
        _persist_never_miss_categories(state, stamped)

        log.info(
            "triage.autonomy_marked",
            run_id=state.get("run_id"),
            user_id=state.get("user_id"),
            decisions=len(stamped),
            never_miss_labelled=labelled,
            no_never_miss_label=unresolvable,
            **{f"n_{k}": v for k, v in tally.items()},
        )
        return {"decisions": stamped, "error": None}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"mark_autonomy_state failed: {exc}"}


def _express_never_miss_as_label(
    decisions: list[dict],
    categories: dict[str, dict],
    items: dict[str, dict],
    *,
    vip: dict,
    sender_stats: dict,
) -> tuple[int, int]:
    """Phase 9. Turn every resolvable never-miss hold into a labelled archive.

    Mutates ``decisions`` in place and returns ``(labelled, unresolvable)``.

    The rules, all of them load-bearing:

    * **Only ``held_by_never_miss``.** ``needs_your_call`` (below the confidence
      floor, or no category fit) and ``below_threshold`` are untouched — the
      reframe archives mail the agent was confident *matters*, never mail it was
      not confident about. Those two buckets are driven to zero by a better
      taxonomy, not by archiving them.
    * **No resolvable label means the thread stays in the inbox.** It is left
      exactly as it was — a ``keep`` — and the remainder ledger names it under
      ``no_never_miss_label``. A bare archive of a never-miss thread is not a
      permitted operation, so an unresolvable label can only ever cost us zero,
      never cost the user a thread.
    * **The verdict is unchanged; only its expression is.** ``autonomy_state``
      stays ``held_by_never_miss`` and ``decided_by`` is not rewritten, so the
      ledger and the history still say *why* — it now reads "why it was
      labelled" rather than "why it stayed".
    """
    labelled = 0
    unresolvable = 0
    for decision in decisions:
        if decision.get("autonomy_state") != "held_by_never_miss":
            continue
        if decision.get("status") == "needs_your_call":
            # Defence in depth: classify_autonomy_state already routes these to
            # `needs_your_call`, so reaching here means something upstream
            # changed. The floor is not negotiable, so refuse rather than guess.
            continue

        key = autonomy.never_miss_category_key(
            decision,
            items.get(decision.get("item_id")),
            vip=vip,
            sender_stats=sender_stats,
        )
        category = categories.get(key) if key else None
        if category is None:
            decision["never_miss_label"] = None
            unresolvable += 1
            continue

        decision["proposed_action"] = "archive"
        decision["category"] = key
        decision["never_miss_label"] = category.get("channel_label_name") or key
        decision["status"] = decision.get("status") or "proposed"
        decision["reasoning"] = (
            f"{decision.get('reasoning', '')} "
            f"{autonomy.never_miss_reasoning(category)}"
        ).strip()
        labelled += 1
    return labelled, unresolvable


def _persist_never_miss_categories(state: TriageState, decisions: list[dict]) -> None:
    """Write the resolved never-miss ``category_id`` onto the already-checkpointed rows.

    Every tier checkpoints its decision the moment it makes it, and
    ``persist_run_results`` deliberately does **not** rewrite ``category_id`` on
    an existing row — re-categorising mail at finalisation time is exactly the
    kind of late, silent change that node is written to refuse.

    The reframe genuinely does re-categorise, though: a reviewer hold on a
    Notifications thread is filed under ``Important``. Without this write the
    reframe is "plumbed but never wired" — the state dict would say ``important``
    while the row still said ``notifications``, and
    :func:`tools.actions.archive_to_never_miss_label` would then correctly refuse
    to archive it, leaving the inbox stuck at exactly the number this phase
    exists to eliminate.

    Best-effort and non-fatal: on failure the rows keep their old category, the
    archive is refused, and the thread stays in the inbox. That is the safe
    direction — the failure costs zero, never a thread.
    """
    run_id = state.get("run_id")
    user_id = state.get("user_id")
    wanted = {
        d["item_id"]: d["category"]
        for d in decisions
        if d.get("never_miss_label") and d.get("item_id") and d.get("category")
    }
    if not run_id or not user_id or not wanted:
        return

    try:
        from sqlalchemy import select

        from db.session import create_db_session
        from graph.persistence import model_for

        decision_cls = model_for("decisions")
        item_cls = model_for("items")
        category_cls = model_for("categories")
        if decision_cls is None or item_cls is None or category_cls is None:
            return

        with create_db_session() as session:
            # Graph state ids are either the persisted item id or the thread id.
            db_id_of: dict[str, str] = {}
            for row in session.execute(
                select(item_cls).where(
                    item_cls.user_id == user_id,
                    (item_cls.id.in_(sorted(wanted)))
                    | (item_cls.external_thread_id.in_(sorted(wanted))),
                )
            ).scalars():
                db_id_of[row.id] = row.id
                if row.external_thread_id:
                    db_id_of[row.external_thread_id] = row.id

            category_id_of = {
                row.key: row.id
                for row in session.execute(
                    select(category_cls).where(category_cls.user_id == user_id)
                ).scalars()
            }

            by_db_item = {
                db_id_of[item_id]: key
                for item_id, key in wanted.items()
                if item_id in db_id_of
            }
            if not by_db_item:
                return

            updated = 0
            for row in session.execute(
                select(decision_cls).where(
                    decision_cls.run_id == run_id,
                    decision_cls.user_id == user_id,
                    decision_cls.item_id.in_(sorted(by_db_item)),
                )
            ).scalars():
                category_id = category_id_of.get(by_db_item.get(row.item_id) or "")
                if category_id and row.category_id != category_id:
                    row.category_id = category_id
                    updated += 1
            session.commit()
            log.info(
                "triage.never_miss_category_persisted",
                run_id=run_id,
                updated=updated,
                resolved=len(by_db_item),
            )
    except Exception as exc:  # noqa: BLE001 - never fail a run over a back-fill
        log.warning(
            "triage.never_miss_category_persist_failed",
            run_id=run_id,
            error=str(exc),
        )


__all__ = ["align_to_category_default", "mark_autonomy_state"]
