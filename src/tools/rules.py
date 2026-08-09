"""Tier 1 (deterministic rules) and tier 2 (learned sender history) matchers.

Pure functions — no I/O, no side effects. Both tiers are free: they resolve the
obvious majority of a mailbox before a single token is spent.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

VALID_ACTIONS = ("keep", "archive", "digest")

DEFAULT_CATEGORY_KEYS = (
    "newsletters",
    "notifications",
    "receipts",
    "outreach",
    "people",
    "urgent",
)

DEFAULT_TAXONOMY: list[dict] = [
    {
        "key": "newsletters",
        "name": "Newsletters",
        "description": "Subscribed bulk mail: newsletters, digests, marketing blasts, "
        "anything with a List-Id or unsubscribe link that the user opted into.",
        "default_action": "archive",
    },
    {
        "key": "notifications",
        "name": "Notifications",
        "description": "Automated app/service notifications: CI results, social updates, "
        "code review pings, calendar noise, status changes.",
        "default_action": "archive",
    },
    {
        "key": "receipts",
        "name": "Receipts",
        "description": "Purchase receipts, invoices, order confirmations, shipping updates, "
        "subscription billing and payment statements.",
        "default_action": "keep",
    },
    {
        "key": "outreach",
        "name": "Outreach",
        "description": "Unsolicited cold outreach: sales pitches, recruiter spam, "
        "partnership requests from strangers.",
        "default_action": "archive",
    },
    {
        "key": "people",
        "name": "People",
        "description": "Genuine person-to-person mail written by a human to this user, "
        "including ongoing conversations and replies.",
        "default_action": "keep",
    },
    {
        "key": "urgent",
        "name": "Urgent",
        "description": "Time-sensitive or high-stakes mail: deadlines, legal notices, "
        "security alerts, account lockouts, overdue invoices.",
        "default_action": "keep",
    },
]

# Tier-2 thresholds.
EVER_REPLIED_CONFIDENCE = 0.97
BULK_ARCHIVE_MIN_RECEIVED = 5
BULK_ARCHIVE_MIN_ARCHIVED = 3
BULK_ARCHIVE_CONFIDENCE = 0.88


def _age_days(item: dict) -> float | None:
    raw = item.get("internal_date")
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if not isinstance(raw, datetime):
        return None
    if raw.tzinfo is None:
        raw = raw.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - raw).total_seconds() / 86400.0


def matches(item: dict, matcher: dict) -> bool:
    """True iff every clause present in ``matcher`` matches ``item``.

    Supported clauses: ``from_email``, ``from_domain``, ``list_id``,
    ``subject_regex``, ``has_attachment``, ``older_than_days``.
    An empty matcher matches nothing (a rule must be specific to fire).
    """
    if not matcher:
        return False

    known = 0
    if (want := matcher.get("from_email")) is not None:
        known += 1
        if (item.get("from_email") or "").lower() != str(want).lower():
            return False
    if (want := matcher.get("from_domain")) is not None:
        known += 1
        domain = (item.get("from_domain") or "").lower()
        want = str(want).lower().lstrip("@")
        if domain != want and not domain.endswith("." + want):
            return False
    if (want := matcher.get("list_id")) is not None:
        known += 1
        list_id = (item.get("list_id") or "").lower()
        if not list_id or str(want).lower() not in list_id:
            return False
    if (want := matcher.get("subject_regex")) is not None:
        known += 1
        try:
            if not re.search(str(want), item.get("subject") or "", re.IGNORECASE):
                return False
        except re.error:
            return False
    if (want := matcher.get("has_attachment")) is not None:
        known += 1
        if bool(item.get("has_attachments")) is not bool(want):
            return False
    if (want := matcher.get("older_than_days")) is not None:
        known += 1
        age = _age_days(item)
        if age is None or age < float(want):
            return False

    return known > 0


def _rule_decision(item: dict, rule: dict) -> dict:
    action = rule.get("action") or {}
    proposed = "archive" if action.get("archive") else ("digest" if action.get("digest") else "keep")
    category = action.get("set_category")
    return {
        "item_id": item["id"],
        "category": category,
        "proposed_action": proposed,
        "confidence": float(rule.get("confidence", 0.9)),
        "reasoning": (
            f"Deterministic rule {rule.get('name', rule.get('id'))!r} matched "
            f"{_evidence(item, rule)}; the rule proposes {proposed}."
        ),
        "decided_by": "rule",
        "rule_id": rule.get("id"),
        "time_sensitive": False,
    }


def _evidence(item: dict, rule: dict) -> str:
    matcher = rule.get("matcher") or {}
    bits = []
    if "list_id" in matcher:
        bits.append(f"List-Id {item.get('list_id')!r}")
    if "from_email" in matcher:
        bits.append(f"sender {item.get('from_email')!r}")
    if "from_domain" in matcher:
        bits.append(f"domain {item.get('from_domain')!r}")
    if "subject_regex" in matcher:
        bits.append(f"subject pattern /{matcher['subject_regex']}/")
    if "has_attachment" in matcher:
        bits.append(f"has_attachments={item.get('has_attachments')}")
    if "older_than_days" in matcher:
        bits.append(f"older than {matcher['older_than_days']} days")
    return ", ".join(bits) or "the item"


def apply_rules(items: list[dict], rules: list[dict]) -> tuple[list[dict], list[dict]]:
    """Tier 1. Returns ``(decisions, unresolved_items)``.

    Only rules with status ``active`` or ``automatic`` are considered; the first
    matching rule (in list order) wins.
    """
    active = [
        r for r in rules or [] if (r.get("status") or "active") in ("active", "automatic")
    ]
    decisions: list[dict] = []
    unresolved: list[dict] = []
    for item in items:
        hit = next((r for r in active if matches(item, r.get("matcher") or {})), None)
        if hit is None:
            unresolved.append(item)
        else:
            decisions.append(_rule_decision(item, hit))
    return decisions, unresolved


def sender_history_decision(item: dict, stats: dict | None) -> dict | None:
    """Tier 2. A decision from learned sender evidence, or ``None`` if inconclusive."""
    if not stats:
        return None

    sender = item.get("from_email") or "(unknown sender)"

    if stats.get("ever_replied"):
        return {
            "item_id": item["id"],
            "category": "people",
            "proposed_action": "keep",
            "confidence": EVER_REPLIED_CONFIDENCE,
            "reasoning": (
                f"You have replied to {sender} before "
                f"({stats.get('replied_count', 0)} replies of "
                f"{stats.get('received_count', 0)} received), so this thread is kept "
                "visible under the reply-history never-miss signal."
            ),
            "decided_by": "sender_history",
            "rule_id": None,
            "time_sensitive": False,
        }

    received = int(stats.get("received_count") or 0)
    opened = int(stats.get("opened_count") or 0)
    archived = int(stats.get("archived_by_user_count") or 0)
    if (
        received >= BULK_ARCHIVE_MIN_RECEIVED
        and opened == 0
        and archived >= BULK_ARCHIVE_MIN_ARCHIVED
        and not stats.get("ever_replied")
    ):
        return {
            "item_id": item["id"],
            "category": "newsletters" if item.get("list_id") else "notifications",
            "proposed_action": "archive",
            "confidence": BULK_ARCHIVE_CONFIDENCE,
            "reasoning": (
                f"Bulk sender {sender}: {received} threads received, none ever opened, "
                f"{archived} archived by you and never a reply — proposing archive on "
                "learned sender history."
            ),
            "decided_by": "sender_history",
            "rule_id": None,
            "time_sensitive": False,
        }

    return None


def apply_sender_history(
    items: list[dict], sender_stats: dict[str, dict]
) -> tuple[list[dict], list[dict]]:
    """Tier 2 over a list. Returns ``(decisions, unresolved_items)``."""
    decisions: list[dict] = []
    unresolved: list[dict] = []
    for item in items:
        stats = (sender_stats or {}).get((item.get("from_email") or "").lower())
        decision = sender_history_decision(item, stats)
        if decision is None:
            unresolved.append(item)
        else:
            decisions.append(decision)
    return decisions, unresolved
