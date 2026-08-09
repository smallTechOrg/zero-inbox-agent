"""Deterministic, local clustering of a run's decisions.

~200 threads collapse into ~30 bulk decisions. Grouping key precedence is
``list_id`` -> ``from_email`` -> ``from_domain`` -> ``category``; the first key that
gathers >= ``MIN_CLUSTER_SIZE`` threads wins and the leftovers fall into per-category
clusters. Every cluster is homogeneous in ``proposed_action`` so a bulk approval is
never ambiguous, and time-sensitive threads are pulled into their own keep cluster.
"""

from __future__ import annotations

MIN_CLUSTER_SIZE = 3
SAMPLE_SUBJECTS = 3

_KEY_LEVELS = (
    ("list", "list_id"),
    ("sender", "from_email"),
    ("domain", "from_domain"),
)


def _pretty_list_id(list_id: str) -> str:
    core = list_id.strip().strip("<>")
    if "." in core:
        parts = [p for p in core.split(".") if p]
        # substack.com -> Substack ; list.foo.substack.com -> Substack
        core = parts[-2] if len(parts) >= 2 else parts[0]
    return core.replace("-", " ").replace("_", " ").title()


def _label(kind: str, value: str, item: dict, category_names: dict[str, str]) -> str:
    if kind == "list":
        return f"{_pretty_list_id(value)} mailing list"
    if kind == "sender":
        name = item.get("from_name") or value
        return f"{name} <{value}>"
    if kind == "domain":
        return f"Mail from {value}"
    return category_names.get(value, (value or "uncategorised").title())


def _stats(decisions: list[dict]) -> tuple[float, float]:
    confidences = [float(d.get("confidence") or 0.0) for d in decisions] or [0.0]
    return min(confidences), sum(confidences) / len(confidences)


def _make_cluster(
    kind: str,
    label: str,
    members: list[dict],
    items_by_id: dict[str, dict],
    action: str,
) -> dict:
    minimum, average = _stats(members)
    subjects: list[str] = []
    for decision in members:
        subject = (items_by_id.get(decision["item_id"], {}).get("subject") or "").strip()
        if subject and subject not in subjects:
            subjects.append(subject)
        if len(subjects) == SAMPLE_SUBJECTS:
            break
    return {
        "kind": kind,
        "label": label,
        "item_count": len(members),
        "suggested_action": action,
        "min_confidence": round(minimum, 4),
        "avg_confidence": round(average, 4),
        "sample_subjects": subjects,
        "item_ids": [d["item_id"] for d in members],
    }


def cluster(
    decisions: list[dict],
    items: list[dict],
    *,
    categories: list[dict] | None = None,
) -> list[dict]:
    """Group ``decisions`` into clusters. Counts always sum to ``len(decisions)``."""
    items_by_id = {i["id"]: i for i in items}
    category_names = {c["key"]: c.get("name", c["key"]) for c in (categories or [])}
    clusters: list[dict] = []

    # Time-sensitive threads never join a bulk-archive cluster.
    time_sensitive = [d for d in decisions if d.get("time_sensitive")]
    pool = [d for d in decisions if not d.get("time_sensitive")]

    if time_sensitive:
        clusters.append(
            _make_cluster(
                "category",
                "Time-sensitive — keep visible",
                time_sensitive,
                items_by_id,
                "keep",
            )
        )

    for kind, field in _KEY_LEVELS:
        buckets: dict[tuple[str, str], list[dict]] = {}
        for decision in pool:
            item = items_by_id.get(decision["item_id"], {})
            value = (item.get(field) or "").strip().lower()
            if not value:
                continue
            buckets.setdefault((value, decision.get("proposed_action") or "keep"), []).append(
                decision
            )

        claimed: set[str] = set()
        for (value, action), members in buckets.items():
            if len(members) < MIN_CLUSTER_SIZE:
                continue
            sample_item = items_by_id.get(members[0]["item_id"], {})
            clusters.append(
                _make_cluster(
                    kind,
                    _label(kind, value, sample_item, category_names),
                    members,
                    items_by_id,
                    action,
                )
            )
            claimed.update(d["item_id"] for d in members)
        pool = [d for d in pool if d["item_id"] not in claimed]

    leftovers: dict[tuple[str, str], list[dict]] = {}
    for decision in pool:
        key = (decision.get("category") or "uncategorised", decision.get("proposed_action") or "keep")
        leftovers.setdefault(key, []).append(decision)
    for (category_key, action), members in leftovers.items():
        clusters.append(
            _make_cluster(
                "category",
                _label("category", category_key, {}, category_names),
                members,
                items_by_id,
                action,
            )
        )

    clusters.sort(key=lambda c: c["item_count"], reverse=True)
    return clusters
