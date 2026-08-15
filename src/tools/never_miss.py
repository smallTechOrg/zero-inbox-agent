"""Mechanisms B (confidence floor) and C (reply-history guard) of never-miss.

See spec/capabilities/never-miss-safeguards.md. Pure functions, no I/O — the
second-pass reviewer (mechanism A) lives in ``graph.nodes_review`` because it
needs the LLM client. The fixed order per the spec is reviewer -> floor ->
reply-history: each stage can only make an outcome MORE conservative, never
re-open something an earlier stage already forced to ``keep``.
"""

from __future__ import annotations

from tools.correspondents import is_no_reply, is_self_address

DEFAULT_CONFIDENCE_FLOOR = 0.75


def is_genuine_correspondent(
    email: str, *, account_email: str = "", aliases: list[str] | None = None
) -> bool:
    """False for the two addresses a reply-history signal can never be about.

    A ``no-reply@`` mailbox does not read replies, and the user is not his own
    correspondent. Either one produces an ``ever_replied`` claim that is an
    artefact of how the evidence was harvested, not evidence of a relationship.
    """
    if not email:
        return False
    if is_no_reply(email):
        return False
    if account_email and is_self_address(
        email, account_email=account_email, aliases=aliases or []
    ):
        return False
    return True


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


def _is_vip_match(vip: dict, *, email: str, domain: str, subject: str) -> bool:
    """Mirrors ``tools.memory.is_vip`` without a DB session, over the grouped VIP dict."""
    email_l = (email or "").lower()
    domain_l = (domain or "").lower()
    subject_l = (subject or "").lower()
    if any(e.lower() == email_l for e in (vip.get("emails") or [])):
        return True
    if any(domain_l == d.lower() or domain_l.endswith("." + d.lower()) for d in (vip.get("domains") or [])):
        return True
    if any(k.lower() in subject_l for k in (vip.get("keywords") or [])):
        return True
    return False


def apply_vip_guard(
    decisions: list[dict],
    vip: dict,
    items: list[dict],
) -> list[dict]:
    """VIP entries (email, domain, keyword) can never be archived automatically,
    at any confidence — the same hard-override treatment as reply-history.

    A VIP match forces ``keep`` regardless of the proposed action or confidence,
    even if an earlier stage already left it as ``needs_your_call``.

    ``vip`` is the grouped dict ``{emails: [], domains: [], keywords: []}`` as
    loaded by ``graph.persistence.load_context``.
    """
    items_by_id = {item["id"]: item for item in items}
    out: list[dict] = []
    for decision in decisions:
        decision = dict(decision)
        item = items_by_id.get(decision.get("item_id"), {})
        matched = _is_vip_match(
            vip or {},
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
    account_email: str = "",
    aliases: list[str] | None = None,
) -> list[dict]:
    """Mechanism C. A sender the user has ever replied to is important.

    Any thread from an ``ever_replied`` sender that is still proposed for
    archive at this point (having survived the reviewer and the floor) is
    forced to ``keep`` — unless the user has created an explicit override
    naming that sender. Checked last, so it cannot be argued away by the
    model or slip past a false-negative the reviewer missed.

    **Defence in depth (Phase 9).** An ``ever_replied`` claim is *ignored* when
    the sender cannot be a genuine correspondent — the user's own address, or a
    ``no-reply@`` machine mailbox. The adapter no longer records such claims
    (:func:`channels.gmail.adapter._accumulate_recipients`), but a
    ``sender_profiles`` row harvested before that fix still asserts one, and a
    stale row must not hold a thread in the inbox forever.
    """
    override_set = {e.lower() for e in (overrides or set())}
    items_by_id = {item["id"]: item for item in items}
    out: list[dict] = []
    for decision in decisions:
        decision = dict(decision)
        item = items_by_id.get(decision.get("item_id"), {})
        sender = (item.get("from_email") or "").lower()
        stats = (sender_stats or {}).get(sender) or {}
        if sender and not is_genuine_correspondent(
            sender, account_email=account_email, aliases=aliases
        ):
            stats = {}
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
