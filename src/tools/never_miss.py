"""Mechanisms B (confidence floor) and C (reply-history guard) of never-miss.

See spec/capabilities/never-miss-safeguards.md. Pure functions, no I/O — the
second-pass reviewer (mechanism A) lives in ``graph.nodes_review`` because it
needs the LLM client. The fixed order per the spec is reviewer -> floor ->
reply-history: each stage can only make an outcome MORE conservative, never
re-open something an earlier stage already forced to ``keep``.
"""

from __future__ import annotations

DEFAULT_CONFIDENCE_FLOOR = 0.75


def apply_confidence_floor(decisions: list[dict], floor: float) -> list[dict]:
    """Mechanism B. Below ``floor`` the agent never archives.

    Any decision whose confidence is below the floor is forced to
    ``proposed_action="keep"`` and ``status="needs_your_call"`` — it stays
    visible in the inbox and is never mutated. Decisions at or above the floor
    are marked ``status="proposed"`` (unless already set to something else by
    an earlier stage).
    """
    out: list[dict] = []
    for decision in decisions:
        decision = dict(decision)
        confidence = float(decision.get("confidence") or 0.0)
        if confidence < floor:
            if decision.get("proposed_action") != "keep":
                decision["reasoning"] = (
                    f"{decision.get('reasoning', '')} Confidence {confidence:.2f} is below "
                    f"the {floor:.2f} floor, so nothing is proposed and this is left for you "
                    "to decide."
                ).strip()
            decision["proposed_action"] = "keep"
            decision["status"] = "needs_your_call"
        else:
            decision.setdefault("status", "proposed")
        out.append(decision)
    return out


def _is_vip_match(entries: list[dict], *, email: str, domain: str, subject: str) -> bool:
    """Mirrors ``tools.memory.is_vip`` without a DB session, over pre-loaded entries."""
    email_l = (email or "").lower()
    domain_l = (domain or "").lower()
    subject_l = (subject or "").lower()
    for entry in entries or []:
        kind = entry.get("kind")
        val = (entry.get("value") or "").lower()
        if not val:
            continue
        if kind == "email" and val == email_l:
            return True
        if kind == "domain" and (domain_l == val or domain_l.endswith("." + val)):
            return True
        if kind == "keyword" and val in subject_l:
            return True
    return False


def apply_vip_guard(
    decisions: list[dict],
    vip_entries: list[dict],
    items: list[dict],
) -> list[dict]:
    """VIP entries (email, domain, keyword) can never be archived automatically,
    at any confidence — the same hard-override treatment as reply-history.

    A VIP match forces ``keep`` regardless of the proposed action or confidence,
    even if an earlier stage already left it as ``needs_your_call``.
    """
    items_by_id = {item["id"]: item for item in items}
    out: list[dict] = []
    for decision in decisions:
        decision = dict(decision)
        item = items_by_id.get(decision.get("item_id"), {})
        matched = _is_vip_match(
            vip_entries,
            email=item.get("from_email") or "",
            domain=item.get("from_domain") or "",
            subject=item.get("subject") or "",
        )
        if matched and decision.get("proposed_action") == "archive":
            decision["reasoning"] = (
                f"{decision.get('reasoning', '')} This sender is on your VIP list, so this "
                "thread is forced to keep regardless of confidence."
            ).strip()
            decision["proposed_action"] = "keep"
            decision["decided_by"] = decision.get("decided_by") or "vip"
            decision["status"] = "proposed"
        out.append(decision)
    return out


def apply_reply_history_guard(
    decisions: list[dict],
    sender_stats: dict[str, dict],
    items: list[dict],
    *,
    overrides: set[str] | None = None,
) -> list[dict]:
    """Mechanism C. A sender the user has ever replied to is important.

    Any thread from an ``ever_replied`` sender that is still proposed for
    archive at this point (having survived the reviewer and the floor) is
    forced to ``keep`` — unless the user has created an explicit override
    naming that sender. Checked last, so it cannot be argued away by the
    model or slip past a false-negative the reviewer missed.
    """
    override_set = {e.lower() for e in (overrides or set())}
    items_by_id = {item["id"]: item for item in items}
    out: list[dict] = []
    for decision in decisions:
        decision = dict(decision)
        item = items_by_id.get(decision.get("item_id"), {})
        sender = (item.get("from_email") or "").lower()
        stats = (sender_stats or {}).get(sender) or {}
        if (
            stats.get("ever_replied")
            and sender
            and sender not in override_set
            and decision.get("proposed_action") == "archive"
        ):
            decision["reasoning"] = (
                f"{decision.get('reasoning', '')} You have replied to {sender} before, so "
                "this thread is forced to keep under the reply-history never-miss signal, "
                "regardless of the classifier's proposal."
            ).strip()
            decision["proposed_action"] = "keep"
            decision["decided_by"] = decision.get("decided_by") or "sender_history"
            decision["status"] = "proposed"
        out.append(decision)
    return out
