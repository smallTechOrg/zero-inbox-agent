"""The autonomy policy — the pure decision logic behind "drive to inbox zero".

spec/capabilities/drive-to-inbox-zero.md is the source of truth. Everything in
this module is a **pure function**: no DB session, no I/O, no LLM. That is
deliberate — the whole policy is unit-testable without a session, and the two
LangGraph nodes in ``graph.nodes_autonomy`` are thin wrappers over it.

Three things live here:

1. ``effective_threshold`` — the per-category autonomy bar, floored by the
   never-miss ``confidence_floor``. The threshold may raise the bar, never lower
   it (Rule A3).
2. ``should_align`` — Rule C1's exclusion list, i.e. "may this decision be moved
   from ``keep`` toward its category's ``default_action``?". This is the only
   place in the system permitted to answer "yes" to that question, and it runs
   *before* every never-miss safeguard so the reviewer, the confidence floor and
   the VIP / reply-history guards all still audit and can veto the result.
3. ``classify_autonomy_state`` — the fixed precedence that stamps each decision
   with exactly one ``autonomy_state``, naming the **first** rule that stopped
   the agent acting. It is the grouping key of the remainder ledger and the sole
   basis of ``distance_to_zero``.

Calibration note (Rule B). ``DEFAULT_AUTO_ACT_THRESHOLD`` is ``0.80``, not the
``0.95`` that shipped before Phase 7. Measured against run ``fbeed060``'s 615
archive proposals: 0 scored ``>= 0.95``, 22 scored ``0.90-0.94``, 522 scored
``0.80-0.89`` and 71 scored ``0.75-0.79``. A 0.95 bar is above the model's entire
achievable range, which is precisely why that run archived nothing.
"""

from __future__ import annotations

from tools.never_miss import DEFAULT_CONFIDENCE_FLOOR, _is_vip_match

#: The global confidence bar at or above which the agent acts on its own.
#: See the calibration table in spec/capabilities/drive-to-inbox-zero.md § B.
DEFAULT_AUTO_ACT_THRESHOLD = 0.80

#: Anything above this is accepted but warned about: the measured model ceiling
#: is ~0.94, so a bar above 0.90 means the agent acts on almost nothing.
MODEL_CEILING_WARNING_THRESHOLD = 0.90

#: The five values that partition a run. Exactly one per decision.
AUTONOMY_STATES = (
    "auto_act",
    "below_threshold",
    "held_by_never_miss",
    "category_keep",
    "needs_your_call",
)

#: Actions that take a thread out of the inbox.
LEAVING_ACTIONS = ("archive", "digest")

#: Phase 9 — the three never-miss labels, in resolution precedence order.
#:
#: ``people`` is a claim about a *relationship* (a VIP, or someone the user has
#: genuinely replied to); ``urgent`` is a claim about *time*; ``important`` is
#: what is left when the second-pass reviewer said "the user would be upset to
#: miss this" for any other reason. They are checked in that order and the first
#: match wins, so the label always names the strongest thing known about the
#: thread rather than whichever guard happened to fire last.
PEOPLE_KEY = "people"
URGENT_KEY = "urgent"
IMPORTANT_KEY = "important"
NEVER_MISS_CATEGORY_KEYS: tuple[str, ...] = (PEOPLE_KEY, URGENT_KEY, IMPORTANT_KEY)

#: The ``autonomy_state`` values whose decisions the apply pass is allowed to act
#: on. Before Phase 9 this was ``auto_act`` alone, and that is precisely why 227
#: threads could never leave the inbox: a never-miss verdict was expressed as
#: "stay put", so the state that named the verdict also blocked the mutation.
#:
#: From Phase 9 a never-miss verdict is expressed as *archive under the label
#: that names the reason*, so ``held_by_never_miss`` is appliable too — but only
#: through :func:`tools.actions.archive_to_never_miss_label`, which refuses to
#: run without a resolved never-miss label. ``auto_act`` keeps its own,
#: unchanged path. Nothing else is ever appliable.
APPLIABLE_AUTONOMY_STATES: frozenset[str] = frozenset({"auto_act", "held_by_never_miss"})

#: Most-conservative-first. Used to break ties when refreshing a cluster action.
_ACTION_CONSERVATISM = ("keep", "digest", "archive")


def _as_float(value, default: float) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def effective_threshold(category: dict | None, settings: dict | None) -> float:
    """The confidence bar this decision must clear for the agent to act alone.

    ``max(category.auto_act_threshold ?? settings.auto_act_threshold,
          settings.confidence_floor)``

    The never-miss floor is a **hard lower bound** (Rule A3): a per-category
    threshold can raise the bar, never lower it. ``category=None`` resolves to
    the settings-only value. Never raises — a malformed value falls back to the
    documented default rather than exploding mid-run.
    """
    settings = settings or {}
    floor = _as_float(settings.get("confidence_floor"), DEFAULT_CONFIDENCE_FLOOR)
    global_bar = _as_float(settings.get("auto_act_threshold"), DEFAULT_AUTO_ACT_THRESHOLD)

    bar = global_bar
    if category:
        override = category.get("auto_act_threshold")
        if override is not None:
            bar = _as_float(override, global_bar)

    return max(bar, floor)


def _sender_email(item: dict | None) -> str:
    return ((item or {}).get("from_email") or "").lower()


def is_vip(item: dict | None, vip: dict | None) -> bool:
    item = item or {}
    return _is_vip_match(
        vip or {},
        email=item.get("from_email") or "",
        domain=item.get("from_domain") or "",
        subject=item.get("subject") or "",
    )


def has_reply_history(item: dict | None, sender_stats: dict | None) -> bool:
    stats = (sender_stats or {}).get(_sender_email(item)) or {}
    return bool(stats.get("ever_replied"))


def should_align(
    decision: dict,
    category: dict | None,
    settings: dict | None,
    sender_stats: dict | None = None,
    vip: dict | None = None,
    *,
    item: dict | None = None,
) -> bool:
    """Rule C1 — may this decision be moved toward its category's default action?

    Every clause below is an exclusion the spec names explicitly. The
    reply-history and VIP clauses are defence in depth: the guards that enforce
    them run later in the never-miss chain and would veto the conversion anyway,
    but a decision that will be vetoed should never have been proposed.
    """
    if not category:
        return False

    default_action = category.get("default_action")
    if default_action not in LEAVING_ACTIONS:
        # Rule A4: a `keep` category is never auto-acted on, at any confidence.
        return False

    if decision.get("proposed_action") == default_action:
        return False  # already there; nothing to convert

    if decision.get("status") == "needs_your_call":
        return False
    if decision.get("decided_by") == "error":
        return False
    if decision.get("decided_by") == "rule":
        # Rule C4: a deterministic rule is the user's own instruction and
        # outranks every category default, in both directions.
        return False
    if decision.get("time_sensitive"):
        return False
    if decision.get("unsure"):
        return False

    confidence = _as_float(decision.get("confidence"), 0.0)
    if confidence < effective_threshold(category, settings):
        return False

    if has_reply_history(item, sender_stats):
        return False
    if is_vip(item, vip):
        return False

    return True


def alignment_reasoning(decision: dict, category: dict, settings: dict | None) -> str:
    """The one sentence appended when a decision is realigned. Names the category
    and the bar, so the history view explains itself without a lookup."""
    bar = effective_threshold(category, settings)
    name = category.get("name") or category.get("key") or "this category"
    action = category.get("default_action")
    confidence = _as_float(decision.get("confidence"), 0.0)
    return (
        f"Your {name} category is set to {action} by default and this thread scored "
        f"{confidence:.2f}, at or above its {bar:.2f} autonomy threshold, so the agent "
        f"proposes to {action} it."
    )


def classify_autonomy_state(
    decision: dict,
    category: dict | None,
    settings: dict | None,
    sender_stats: dict | None = None,
    vip: dict | None = None,
    *,
    item: dict | None = None,
) -> str:
    """Stamp exactly one ``autonomy_state``, under the fixed precedence in
    spec/agent.md § Nodes → ``mark_autonomy_state``.

    The order matters: the value always names the **first** rule that stopped the
    agent acting, so the remainder ledger reads as a causal explanation rather
    than an arbitrary bucketing.
    """
    # 1. The user has to decide this one.
    if decision.get("status") == "needs_your_call" or decision.get("decided_by") == "error":
        return "needs_your_call"

    # 2. A never-miss safeguard is what is holding it in the inbox.
    if (
        decision.get("decided_by") == "reviewer"
        or decision.get("time_sensitive")
        or is_vip(item, vip)
        or has_reply_history(item, sender_stats)
    ):
        return "held_by_never_miss"

    action = decision.get("proposed_action")

    # 3. The category (or the user's own rule) says this kind of mail stays.
    if category and category.get("default_action") not in LEAVING_ACTIONS:
        return "category_keep"
    if decision.get("decided_by") == "rule" and action == "keep":
        return "category_keep"

    # 4. The agent is confident enough to act on its own.
    if action in LEAVING_ACTIONS:
        if _as_float(decision.get("confidence"), 0.0) >= effective_threshold(category, settings):
            return "auto_act"

    # 5. Everything else: above the floor, under the bar.
    return "below_threshold"


def never_miss_category_key(
    decision: dict,
    item: dict | None,
    *,
    vip: dict | None = None,
    sender_stats: dict | None = None,
) -> str | None:
    """Phase 9 — which never-miss label expresses this verdict? First match wins.

    ``spec/capabilities/never-miss-safeguards.md § Phase 9 — how a never-miss
    verdict is expressed``:

    1. VIP match or ``ever_replied`` (post-correspondent-truth) -> ``people``
    2. ``time_sensitive`` -> ``urgent``
    3. a reviewer flip for any other reason -> ``important``
    4. otherwise -> ``None``

    Deterministic and free: no LLM, no session, no I/O. ``None`` is a real
    answer, not a failure — it means *this thread stays in the inbox and the
    ledger names it under* ``no_never_miss_label``. Silence is never the
    fallback: an unresolvable label must never become a bare archive.

    The caller is responsible for only asking about decisions whose
    ``autonomy_state`` is ``held_by_never_miss``. Asking about any other
    decision is meaningless — a VIP thread the agent is confident about is an
    ``auto_act`` archive under its *own* category, not a never-miss label.
    """
    if is_vip(item, vip) or has_reply_history(item, sender_stats):
        return PEOPLE_KEY
    if decision.get("time_sensitive"):
        return URGENT_KEY
    if decision.get("decided_by") == "reviewer":
        return IMPORTANT_KEY
    return None


def never_miss_reasoning(category: dict) -> str:
    """The one plain sentence appended when a never-miss verdict becomes a label.

    It names the Gmail label, so the history view and the thread itself both
    explain, without a lookup, where the mail went and that it is one click
    away rather than gone.
    """
    label = (
        category.get("channel_label_name")
        or f"ZeroInbox/{category.get('name') or category.get('key') or 'Important'}"
    )
    return (
        f"This is mail you must not miss, so instead of leaving it in your inbox the "
        f"agent files it under {label}, where it stays one click away and can be put "
        "back at any time."
    )


def _most_conservative(actions: list[str]) -> str:
    for action in _ACTION_CONSERVATISM:
        if action in actions:
            return action
    return "keep"


def refresh_cluster_actions(clusters: list[dict], decisions: list[dict]) -> list[dict]:
    """Rule C5 — recompute each cluster's ``suggested_action`` from its members.

    ``cluster_decisions`` runs *before* the autonomy stage, so its suggested
    actions are stale the moment a member is realigned. Majority action wins; a
    tie resolves to the more conservative action. Mutated in place (and returned)
    so no stale cluster action is ever persisted.
    """
    action_by_item = {
        d.get("item_id"): d.get("proposed_action", "keep") for d in (decisions or [])
    }
    for cluster in clusters or []:
        member_actions = [
            action_by_item[item_id]
            for item_id in (cluster.get("item_ids") or [])
            if item_id in action_by_item
        ]
        if not member_actions:
            continue
        tally: dict[str, int] = {}
        for action in member_actions:
            tally[action] = tally.get(action, 0) + 1
        top = max(tally.values())
        cluster["suggested_action"] = _most_conservative(
            [action for action, count in tally.items() if count == top]
        )
    return clusters or []


__all__ = [
    "APPLIABLE_AUTONOMY_STATES",
    "AUTONOMY_STATES",
    "DEFAULT_AUTO_ACT_THRESHOLD",
    "IMPORTANT_KEY",
    "LEAVING_ACTIONS",
    "MODEL_CEILING_WARNING_THRESHOLD",
    "NEVER_MISS_CATEGORY_KEYS",
    "PEOPLE_KEY",
    "URGENT_KEY",
    "alignment_reasoning",
    "never_miss_category_key",
    "never_miss_reasoning",
    "classify_autonomy_state",
    "effective_threshold",
    "has_reply_history",
    "is_vip",
    "refresh_cluster_actions",
    "should_align",
]
